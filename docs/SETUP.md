# 설치와 첫 실행

명령은 저장소 루트의 PowerShell 기준이며 어느 폴더에 복제해도 된다.
Windows / 64비트 CPython 3.13.15에서 호스트 설치·오프라인 검증을 확인했다.
GPU 배포는 RTX 5080 / Blackwell sm120 프로필이다. 다른 OS·Python·GPU 조합은 별도 검증 대상이다.

## 1. 호스트 설치와 오프라인 검증

Git으로 저장소를 복제한 뒤 해당 폴더에서 실행한다. 검증 버전은
[constraints-tested.txt](../constraints-tested.txt)에 있다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints-tested.txt
.\.venv\Scripts\python.exe run.py doctor
.\.venv\Scripts\python.exe tools/check_architecture.py
.\.venv\Scripts\python.exe run.py test
```

실제 사용만 할 때는 `requirements-dev.txt` 대신 `requirements.txt`를 설치한다.
이후 `python`은 `.\.venv\Scripts\python.exe`로 바꾸거나 해당 환경을 활성화한다.
실행 파일을 직접 쓰면 PowerShell 실행 정책을 바꿀 필요가 없다.

최초 pip 설치·이미지·모델 다운로드에는 네트워크가 필요하다. 설치 후 오프라인 시험은
PaddleOCR·모델·GPU·Docker·원본 책 없이 실행된다. `doctor`도 호스트 패키지만 검사하며,
`--json`을 추가하면 기계 판독 결과를 출력한다.

## 2. GPU·Docker 이미지 준비

Windows에서는 NVIDIA 드라이버와 GPU 지원이 설정된 Docker Desktop의 Linux 컨테이너/WSL2가 필요하다.
GPU·CUDA·드라이버 조건은 공식 [Blackwell 안내](https://www.paddleocr.ai/v3.5.0/en/version3.x/pipeline_usage/PaddleOCR-VL-NVIDIA-Blackwell.html)와
[PaddleOCR-VL 배포 안내](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html)를 따른다.
Docker에서 GPU가 보이는지 먼저 확인한다. 현재 이미지·모델은 저장소에 포함하지 않는다.

개인 설정이 없을 때만 예제를 복사한다. 아래 `pull`은 이미지를 다운로드하며 CLI는 자동 pull하지 않는다.

```powershell
if (-not (Test-Path compose/.env)) {
    Copy-Item compose/.env.example compose/.env
}
docker compose --project-directory compose -f compose/compose.yaml config --quiet
docker compose --project-directory compose -f compose/compose.yaml pull
```

`.env.example`은 `latest-nvidia-gpu-sm120-offline` 프로필을 다음 digest로 고정한다.
로컬 이미지의 registry digest를 2026-10-01에 확인했다. 변동 가능한 태그만으로 재현하지 않는다.

| 이미지 (`ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/` 아래) | 고정 digest |
|---|---|
| `paddleocr-vl` (문단·줄 API) | `sha256:0971c409d1cab2b12aa17b76855e36ac8eb9fb1adc97dbeea15e9b09432a4a3b` |
| `paddleocr-genai-vllm-server` (VLM) | `sha256:bffd525308facf5dba2f8eca44ab476704a0ae3bfdcba25f77655973e4c0a7ca` |

예제 설정으로 선택되는 이미지와 다운로드한 이미지의 digest는 다음 **읽기 전용** 명령으로 확인한다.

```powershell
$images = docker compose --project-directory compose --env-file compose/.env.example -f compose/compose.yaml config --images | Sort-Object -Unique
foreach ($image in $images) { docker image inspect $image --format '{{json .RepoDigests}}' }
```

처음 복사한 설정은 원래 GPU 프로필·태그와 이미지 바이트 식별자를 함께 고정한다.
기존 개인 `.env`는 보존하며, 재현 프로필을 적용하려면 OCR 종료 후 예제의 두 image suffix를 대조한다.
다른 이미지의 digest를 혼용하거나 운영 중 서비스를 재시작하지 않는다.

Compose 공유 메모리는 최대 64 GiB, 호스트 CPU 준비 예산 기본값은 16 GiB다.
실제 상시 사용량이나 검증된 최소 사양은 아니다. RAM/VRAM이 부족하면 작은 문서로 시작하고
[성능 설정](PERFORMANCE.md)의 작업자·준비 예산을 낮춘다.

## 3. 공식 줄 모델의 정확한 파일 다운로드

다음 공식 revision은 2026-10-01에 조회했고, 현재 로컬의 세 추론 파일 모두와 일치했다.
JSON/YAML은 Git blob ID, 가중치는 공식 LFS SHA-256으로 대조했다.

| 모델·로컬 폴더 (`models/official_models/` 아래) | 고정 revision |
|---|---|
| [PP-OCRv5_mobile_rec](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_rec/tree/682f20538d8c086cb2128e5cfac775e6c4904e85) | `682f20538d8c086cb2128e5cfac775e6c4904e85` |
| [korean_PP-OCRv5_mobile_rec](https://huggingface.co/PaddlePaddle/korean_PP-OCRv5_mobile_rec/tree/c02ecaf1f22bfd1c618cce154fd19185b47e663a) | `c02ecaf1f22bfd1c618cce154fd19185b47e663a` |

아래 명령은 모델마다 `inference.json`, `inference.pdiparams`, `inference.yml`만 받는다.
기존 파일도 SHA-256을 검사하고, 내용이 다르면 덮어쓰지 않고 중단한다. 파일 이름만 같은 다른 모델로
바꾸지 않는다. 다운로드 실패 파일은 임시 이름으로 남으며 기존 모델·운영 캐시는 바꾸지 않는다.

```powershell
$ErrorActionPreference = 'Stop'
$modelSpecs = @(
    @{
        Name = 'PP-OCRv5_mobile_rec'
        Revision = '682f20538d8c086cb2128e5cfac775e6c4904e85'
        Hashes = @{
            'inference.json' = '24587345250c7332d0fc6f9a44e794d078cdaeb64c302fef906f325619de2569'
            'inference.pdiparams' = '2460da90875937c94db97eba74ae3d9e5d4c4c57c42f1f41531c09a26bcc771a'
            'inference.yml' = '5dfeb2777f6d0db8177d8128a8acfcf6e6276dc4ac73ea3bf0dc06d6a5e85d8e'
        }
    },
    @{
        Name = 'korean_PP-OCRv5_mobile_rec'
        Revision = 'c02ecaf1f22bfd1c618cce154fd19185b47e663a'
        Hashes = @{
            'inference.json' = '562404e3c590c50c93778d5f0a94df21b47b5ab8f3ea6d47c7f8a7930c3bc844'
            'inference.pdiparams' = 'cac3e5f12cf04aaa77f6a5bc704e4e736ef2908476551891d84b41b4e9090462'
            'inference.yml' = 'f757fa1c40e99edcf27e9cce879b93eb2a51fa46f5ef39095689b8c37dd75998'
        }
    }
)
foreach ($model in $modelSpecs) {
    $folder = Join-Path 'models/official_models' $model.Name
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    foreach ($file in 'inference.json', 'inference.pdiparams', 'inference.yml') {
        $target = Join-Path $folder $file
        if (Test-Path -LiteralPath $target) {
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $model.Hashes[$file]) {
                throw "Existing model differs: $target; preserve it and use a separate checkout."
            }
            continue
        }
        $download = "$target.$([Guid]::NewGuid().ToString('N')).download"
        $uri = "https://huggingface.co/PaddlePaddle/$($model.Name)/resolve/$($model.Revision)/$file"
        Invoke-WebRequest -Uri $uri -OutFile $download
        if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $model.Hashes[$file]) {
            throw "Model checksum failed: $download"
        }
        Move-Item -LiteralPath $download -Destination $target
    }
}
```

문단 인식은 Compose의 `PaddleOCR-VL-1.6-0.9B` 서버가 담당하며 offline 이미지의 준비된 모델을 사용한다.
모델·이미지의 권리와 조사 범위는 [의존성 안내](DEPENDENCIES.md)를 따른다.

## 4. 서비스 확인과 첫 PDF

최초 준비나 중단된 환경에서만 시작한다. 이미 OCR이 실행 중이면 `up`·이미지 교체·재시작을 실행하지 않는다.

```powershell
docker compose --project-directory compose -f compose/compose.yaml up -d
python run.py doctor --services
```

`doctor --services`는 모델 파일·Compose·Docker·HTTP health를 읽기 전용으로 검사한다.
서비스를 시작·재시작하거나 OCR을 요청하지 않으며, 파일 존재/health 성공은 추론 성공을 뜻하지 않는다.
8081의 `models`에 한국어/기본 인식기가 있어야 한다. 포트 노출과 인증 부재는 [SECURITY](../SECURITY.md)를 확인한다.

권한이 있는 작은 스캔 PDF를 `input/book.pdf`에 준비한 뒤 실행한다.

```powershell
python run.py "input/book.pdf" --out "output/book"
python run.py audit "output/book"
python run.py export "output/book/.ocr/book_line_ocr.sqlite3" "output/book/exported"
```

완성본 `book_auto_searchable.pdf`를 열어 검색·복사·읽기 순서를 확인하고, 내보낸
`book_auto_report.md`·JSON의 경고 페이지를 원본과 대조한다. audit는 품질 신호이며 정답률 측정은 아니다.
`--fast`는 최종 검증을 생략하므로 첫 검증 실행에 쓰지 않는다.

## 문제 해결

| 증상 | 확인할 사항 |
|---|---|
| 패키지 누락 | 위 가상환경 Python으로 `doctor`와 설치 명령 실행 |
| 서비스 준비 실패 | `doctor --services`, Docker GPU 환경·이미지·개인 `.env` 확인 |
| 한국어 줄 모델 실패 | 두 모델의 세 파일과 8081 health의 모델 목록 확인 |
| 메모리 압박 | `--cpu-workers 2 --cpu-memory-mb 2048 --verify-workers 1` 등으로 호스트 준비량 축소 |
| 캐시 불일치 | 원본/API 식별자가 다르면 기존 캐시 보존 후 별도 출력 사용; 억지로 `--trust-cache`하지 않음 |
| 검증 실패 | 실패 보고서·`.partial.pdf` 확인. 기존 완성본·캐시 보존 후 같은 명령으로 재시도 |
| 파일이 처리 중 | 해당 출력 작업 완료를 기다림. 실행 중 잠금·DB를 삭제하지 않음 |
| 글리프 누락 | 누락 코드포인트·설치 폰트 확인. 임의 문자 대체로 성공 처리하지 않음 |

전체 옵션은 `python run.py input/book.pdf --help`와 [사용 안내](OCR_USAGE.md)를 참고한다.
