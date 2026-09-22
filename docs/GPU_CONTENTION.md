# GPU 전체 사용률과 OCR 처리 성능 구분

## 2026-09-14 새 측정

WSL 재시작 및 긴 획 탐지 개선 후, 같은 32페이지를 새 진단 캐시로 전체 OCR했다.
완료 324.925초, 검증 실패 없음, 기존 결과와 화면 픽셀 동일. 이전 165.802초보다 느렸지만
이 비교는 호스트 부하와 모델 워밍업을 통제하지 않았다. 코드 퇴행/개선 수치로 사용하면 안 된다.

로그 경계 기준 본문 약 200.44초, 유실 보완 42.23초, 경계 보완 43.07초,
overlay 33.40초, 검증/게시 5.64초였다. 인접 저장/준비 비용이 섞인 구간이며 함수별 정밀 타이머는 아니다.
본문의 유효 GPU 표본 197개 평균은 90.9%, 그중 170개가 90% 이상이었다.
따라서 높은 전체 GPU 사용률이 높은 OCR 처리량을 보장하지 않았다.

Windows 프로세스별 GPU 엔진을 직접 읽은 한 표본에서는 **Chrome 78%, WSL VM 16%**가
동일 GPU/3D 엔진에 기록됐다. 이는 해당 표본이지 전체 시간 평균이 아니다. 사용자도 Chrome에서
GPU 작업을 수행 중이었다고 확인했고, 직접 부하를 낮췄다. 다른 앱은 자동 종료하지 않았다.

서버/모델/입력 설정을 바꾸지 않은 동일 10페이지 바이트 반복:

| 조건 | 1회 | 2회 | 3회 | 중앙값 |
|---|---:|---:|---:|---:|
| 부하 축소 전 | 35.414초 | 21.393초 | 20.289초 | 21.393초 |
| 사용자가 부하를 낮춘 뒤 | 12.564초 | 13.359초 | 12.622초 | 12.622초 |

입력 SHA-256은 두 경우 모두 `310041e180dbbedefcb5b6a30ad4341194ca3ec851c00602e607a8ba81963828`.
낮은 부하에서 약 41% 짧았지만 A/B/A로 부하를 재현한 실험은 아니다. 워밍업/호스트 메모리/
모델 응답 변동도 있으므로 전부 Chrome 효과라고 단정하지 않는다. 이는 서버 메모리 설정 전후 비교가 아니다.

두 반복 실험 모두 문자열 변동이 있어 엄격 동일성 종료 코드는 1이다. HTTP 요청은 완료됐으며
오류를 성공으로 숨기지 않는다. 부하 축소 뒤에도 p3 상수 등이 달라지는 기존 모델 변동은 남아 있다.

### 부하 축소 후 32페이지 전체 재측정

같은 코드/설정, 새 진단 캐시의 전체 실행은 **173.629초**에 완료됐다. 이전 부하 중 실행
324.925초보다 약 46.6% 짧았다. 부하를 다시 올리는 교차 실험은 하지 않았으므로 일반화된
속도 보장이나 코드 변경의 효과로 계산하지 않는다.

로그 구간은 본문 70.90초, 유실 보완 41.66초, 경계 보완 27.58초, overlay 27.01초,
검증/게시 6.29초다. CPU 준비/검증은 각각 실제 5개 작업자로 32페이지를 처리했고 복구 0회.
GPU 공급 33요청/682이미지, 오류 0회, 부모의 GPU 응답 대기 합계 약 1.16초였다.
Chrome GPU 엔진의 기록된 최고 사용률은 6%로 내려갔고 WSL VM 엔진은 최고 88%였다.
이 최고값들은 동일 시점이 아니며 엔진별 값을 합산하지 않는다.

낮은 부하에서 유실 보완의 41개 유효 표본 중 GPU ≤10%는 7개였고, 모두 VLM 실행/대기
요청이 없었다. 앞선 CPU 단독 보완 준비 35.868초와 함께 보면 다음 대상은 이 단계의
순차 CPU 탐색/준비다. 함수별 프로파일과 제한된 페이지 병렬화의 동일성 검증이 필요하다.

최종 검증 실패 없음, 전 페이지 화면 픽셀은 같지만 새 인식 p1/3/6/11/22의 글자·좌표는
이전 실행과 달랐다. 원시 문단 문자열부터 p1/3/6/11/22/30에서 다르고 그 단계 박스는 같았다.
공통 줄 응답 771개는 모두 같다. 엄격 PDF 비교 종료 코드 1을 보존했다.
자료는 `low-host-load-full-{output,profile}/`,
`low-host-load-full-{comparison,phases,input-variation}.json`이다.

## 정확도와 변경 범위

324.925초 전체 출력은 이전 새 인식과 p1/6/15/22/31의 글자·좌표가 달랐다.
원시 문단 결과부터 p1/6/15/22/30/31에서 문자열이 달랐고, 그 단계의 박스는 같았다.
공통 줄 이미지 키 771개의 응답은 모두 같았다. 이것은 입력 변동의 위치를 보여 주며,
새 OCR의 정답률 유지나 특정 원인의 인과 증명은 아니다. 고정 입력 CPU 변경의 동일성 증거는
`RULE_RUN_OPTIMIZATION.md`의 별도 검사다.

이번 측정 중 전용 VRAM이 약 15.7GB까지 찼으므로 예약량 축소를 검토했으나,
다른 앱 GPU 부하를 먼저 확인해 **vllm_config.yml의 0.8은 변경하지 않았다**.
`gpu_memory_utilization`은 연산 사용률 목표가 아니라 모델 실행기의 GPU 메모리 예산이다.
[설치 버전 vLLM 0.10.2 문서](https://docs.vllm.ai/en/v0.10.2/cli/bench/throughput.html)를 참고한다.
공유 GPU 메모리 카운터가 있다는 사실만으로 OCR 모델의 페이지 아웃/스와핑을 확정하지 않는다.
[NVIDIA WSL 지침](https://docs.nvidia.com/cuda/wsl-user-guide/)에도 NVML 질의 지원 제한이 있으므로,
Windows 엔진 지표, VLM 실행/대기열, 전력과 실제 처리 시간을 함께 본다.

## 재현 도구

`profile_ocr_resources.py --windows-gpu`는 Windows의 프로세스별 GPU 엔진 및 어댑터 메모리를
5초 간격으로 추가 기록한다. WMI 실패는 OCR을 중단하지 않는다. CPU/기존 NVIDIA/VLM 기록은
그대로이고, 프로세스별 엔진 사용률을 서로 더해 가상의 GPU 총사용률을 만들지 않는다.
모니터가 반복 보관한 최신 Windows 값은 독립 표본이나 정확한 유휴 초 수가 아니다.
새 분석 도구 회귀 5개, 파일 80개/구현 모듈 23개 구조 검사가 통과했다. 실제 Windows
probe 12초 실행도 완료했고 빈 대기 프로그램을 종료한 뒤 별도 모니터 프로세스를 남기지 않았다.

```powershell
python tools/profile_ocr_resources.py --out tmp/NEW-profile --windows-gpu -- python -u compose/ocr_to_searchable_pdf.py tmp/cpu_workers/production32/source.pdf --out tmp/NEW-output
python tools/summarize_ocr_profile.py tmp/NEW-profile --out tmp/NEW-phases.json
python tools/analyze_ocr_input_variation.py --baseline tmp/performance-max/server-reader-full-output --candidate tmp/NEW-output --stem source --out tmp/NEW-input-variation.json
```

원시 자료는 `tmp/performance-max/rule-runs-full-{output,profile}/`,
`rule-runs-full-{comparison,phases,input-variation}.json`,
`vram08-repeat[-profile]/`, `low-host-load-repeat[-profile]/`에 보관했다.
`vram08` 이름은 현재 설정 0.8의 기록일 뿐, VRAM 튜닝이 적용됐다는 의미가 아니다.
