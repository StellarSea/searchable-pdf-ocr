"""Profile an explicitly supplied OCR command without changing its settings.

Only the child command can write OCR outputs; use a fresh diagnostic --out there.
This monitor records host/process CPU, memory, disk, NVIDIA GPU and VLM queues.
No service restarts, model loading, affinity changes or synthetic GPU load.
"""
import argparse
from collections import deque
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import psutil


CREATE_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
GPU_FIELDS = ('gpu_percent', 'memory_percent', 'memory_mib', 'power_watts')
# Device-wide utilization can be dominated by another application. Preserve the
# Windows per-process/per-engine counters rather than attributing it all to OCR.
WINDOWS_GPU_PROBE = r'''
$engineRows = Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine -ErrorAction Stop |
    Where-Object UtilizationPercentage -gt 0
$processNames = @{}
Get-Process | ForEach-Object { $processNames[[string]$_.Id] = $_.ProcessName }
$entries = @($engineRows | ForEach-Object {
    $enginePid = $null
    if ($_.Name -match '^pid_(\d+)_') { $enginePid = [int]$Matches[1] }
    [ordered]@{name=$_.Name; pid=$enginePid; process=$processNames[[string]$enginePid]; utilization=$_.UtilizationPercentage}
})
$adapters = @(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory -ErrorAction Stop |
    Select-Object Name,DedicatedUsage,SharedUsage,TotalCommitted)
[ordered]@{engines=$entries; adapters=$adapters} | ConvertTo-Json -Depth 5 -Compress
'''
def parse_vlm_metrics(data):
    """Keep finish reasons separate so long/aborted generations are observable."""
    import re
    values = {}
    counters = ('vllm:num_requests_running', 'vllm:num_requests_waiting',
                'vllm:kv_cache_usage_perc', 'vllm:gpu_cache_usage_perc',
                'vllm:request_generation_tokens_sum', 'vllm:request_generation_tokens_count',
                'vllm:num_preemptions_total')
    for line in data.splitlines():
        key = line.split('{', 1)[0].split(' ', 1)[0]
        if key == 'vllm:request_success_total':
            reason = re.search(r'finished_reason="([^"]+)"', line)
            if reason is None:
                continue
            key += ':' + reason.group(1)
        elif key not in counters:
            continue
        value = float(line.rsplit(' ', 1)[-1])
        values[key] = values.get(key, 0) + value
    return values


# Send the same parser tested locally to the container; no package installation
# or model import is required for this read-only metrics request.
VLM_PROBE = inspect.getsource(parse_vlm_metrics) + '''
import json,urllib.request
try:
    data=urllib.request.urlopen('http://127.0.0.1:8080/metrics',timeout=3).read().decode()
    print(json.dumps(parse_vlm_metrics(data)),flush=True)
except Exception as error:
    print(json.dumps({'error':str(error)}),flush=True)
'''


def gpu_values(line):
    parts = line.strip().split(',')
    if len(parts) != len(GPU_FIELDS):
        return None
    try:
        return dict(zip(GPU_FIELDS, (float(p.strip()) for p in parts)))
    except ValueError:
        return None


def stop_owned(process):
    """Never target unrelated OCR/Docker processes."""
    if process is None or process.poll() is not None:
        return
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.Error:
            pass
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class Probe:
    def __init__(self, command, decode):
        self.decode, self.latest, self.process = decode, None, None
        self.error = None
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                creationflags=CREATE_FLAGS)
        except OSError as error:
            self.error = str(error)
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        if self.process is None:
            return
        for line in self.process.stdout:
            try:
                value = self.decode(line)
            except (ValueError, TypeError):
                continue
            if value is not None:
                self.latest = (time.monotonic(), value)

    def snapshot(self):
        latest = self.latest
        if latest is None:
            return {'unavailable': self.error or 'no sample'}
        return {'age_seconds': time.monotonic()-latest[0], **latest[1]}

    def close(self):
        stop_owned(self.process)
        self.thread.join(timeout=5)
        if self.process is not None and self.process.stdout is not None:
            self.process.stdout.close()


class PollingProbe(Probe):
    """One bounded remote read per poll; never leave a container loop running."""
    def __init__(self, command, decode, *, interval=1, timeout=5):
        self.command, self.decode = command, decode
        self.interval, self.timeout = interval, timeout
        self.latest, self.error, self.process = None, None, None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        while not self.stop.is_set():
            try:
                result = subprocess.run(self.command, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=self.timeout, creationflags=CREATE_FLAGS)
                if result.returncode:
                    self.error = result.stderr.strip()
                else:
                    self.latest = (time.monotonic(), self.decode(result.stdout))
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                self.error = str(error)
            self.stop.wait(self.interval)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=self.timeout+1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--no-vlm', action='store_true')
    parser.add_argument('--windows-gpu', action='store_true',
                        help='Also sample Windows per-process GPU engines and adapter memory (5s polling)')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.interval < 0.2:
        parser.error('Supply a command after --; interval must be at least 0.2 seconds')
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out/'manifest.json').write_text(json.dumps({
        'command': command, 'cpu_logical': psutil.cpu_count(),
        'cpu_physical': psutil.cpu_count(logical=False),
        'host_memory_bytes': psutil.virtual_memory().total,
        'interval_seconds': args.interval,
        'windows_gpu_requested': args.windows_gpu,
        'scope': 'Host counters include other applications. RSS sums may double-count shared pages.'
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    probes, child, reader = {}, None, None
    events = deque()
    started = time.monotonic()
    try:
        if shutil.which('nvidia-smi'):
            probes['gpu'] = Probe(['nvidia-smi',
                '--query-gpu=utilization.gpu,utilization.memory,memory.used,power.draw',
                '--format=csv,noheader,nounits', '--loop-ms=1000'], gpu_values)
        if not args.no_vlm and shutil.which('docker'):
            probes['vlm'] = PollingProbe(['docker', 'exec', 'paddleocr-vlm-server',
                                  'python', '-u', '-c', VLM_PROBE], json.loads)
        if args.windows_gpu and os.name == 'nt' and shutil.which('powershell.exe'):
            probes['windows_gpu'] = PollingProbe(['powershell.exe', '-NoProfile', '-NonInteractive',
                '-Command', WINDOWS_GPU_PROBE], json.loads, interval=5, timeout=10)
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', creationflags=CREATE_FLAGS,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})

        def read_output():
            with (args.out/'command.log').open('x', encoding='utf-8') as log:
                for line in child.stdout:
                    log.write(line)
                    log.flush()
                    events.append({'elapsed': time.monotonic()-started, 'line': line.rstrip()})

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        process = psutil.Process(child.pid)
        psutil.cpu_percent(percpu=True)
        samples = 0
        with (args.out/'samples.jsonl').open('x', encoding='utf-8') as output:
            while True:
                memory = psutil.virtual_memory()
                disk = psutil.disk_io_counters()
                rss, cpu_seconds, processes = 0, 0.0, 0
                try:
                    tree = [process, *process.children(recursive=True)]
                except psutil.Error:
                    tree = []
                for item in tree:
                    try:
                        rss += item.memory_info().rss
                        cpu = item.cpu_times()
                        cpu_seconds += cpu.user+cpu.system
                        processes += 1
                    except psutil.Error:
                        continue
                lines = []
                while events:
                    lines.append(events.popleft())
                sample = {'elapsed': time.monotonic()-started,
                    'host_cpu_per_core': psutil.cpu_percent(percpu=True),
                    'available_memory_bytes': memory.available,
                    'process_tree_rss_bytes': rss, 'process_tree_cpu_seconds': cpu_seconds,
                    'processes': processes,
                    'disk': disk._asdict() if disk is not None else None,
                    'events': lines, **{name: probe.snapshot() for name, probe in probes.items()}}
                output.write(json.dumps(sample, ensure_ascii=False)+'\n')
                output.flush()
                samples += 1
                if child.poll() is not None and not reader.is_alive():
                    break
                time.sleep(args.interval)
        result = {'seconds': time.monotonic()-started, 'exit_code': child.returncode,
                  'samples': samples, 'monitoring_overhead_included': True}
        (args.out/'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
    finally:
        stop_owned(child)
        if reader is not None:
            reader.join(timeout=5)
        if child is not None and child.stdout is not None:
            child.stdout.close()
        for probe in probes.values():
            probe.close()
    raise SystemExit(child.returncode)


if __name__ == '__main__':
    main()
