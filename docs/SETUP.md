# 설치와 첫 실행

모든 명령은 저장소 루트의 PowerShell 기준이다. 프로젝트를 반드시 `C:\ocr`에 둘 필요는 없다.
기존 실험 문서의 절대 경로는 당시 환경의 예시이며 자신의 경로로 바꾼다.

## 1. 호스트 Python

현재 검증 환경은 Windows / CPython 3.13.15다. Python 패키지 버전은
[constraints-tested.txt](../constraints-tested.txt)에 기록했다. 다른 버전/OS는 별도 검증 대상이다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints-tested.txt
.\.venv\Scripts\python.exe tools/check_architecture.py
.\.venv\Scripts\python.exe run.py test
```

실제 사용만 할 때는 `requirements-dev.txt` 대신 `requirements.txt`를 설치한다.
이후 예제의 `python`은 `.\.venv\Scripts\python.exe`로 바꾸거나 해당 가상환경을 활성화한다.
실행 정책을 바꿀 필요 없이 위와 같이 가상환경 실행 파일을 직접 사용할 수 있다.

오프라인 테스트에 필요한 것은 설치된 호스트 패키지뿐이다. PaddleOCR, GPU, 모델,
Docker 또는 원본 책은 필요 없다. 반면 최초 pip 설치와 아래 이미지/모델 다운로드에는
네트워크가 필요하다.

## 2. 실제 OCR의 GPU 환경

현재 Compose는 NVIDIA Blackwell sm120 / RTX 5080에서 사용한 설정이다.
Windows에서는 GPU 지원이 설정된 Docker Desktop Linux 컨테이너/WSL2 환경이 필요하다.
서로 다른 GPU·CUDA·드라이버 조합은 상위 프로젝트의
[Blackwell 안내](https://www.paddleocr.ai/v3.5.0/en/version3.x/pipeline_usage/PaddleOCR-VL-NVIDIA-Blackwell.html)와
[PaddleOCR-VL 배포 안내](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html)를 확인한다.

Compose의 공유 메모리 설정은 최대 64 GiB이고, 호스트 CPU 준비 예산 기본값은 16 GiB다.
이 수치는 실제 상시 사용량이나 검증된 최소 사양이 아니다. RAM/VRAM이 부족하면
작은 문서로 시작하고 CPU 준비 예산을 낮춘다. 모델 크기·인식 입력 폭·임계값 변경은
별도 정확도 비교 없이 성능 조정으로 취급하지 않는다.

개인 설정 파일이 없을 때만 예제를 복사한다. 기존 설정을 덮어쓰지 않는다.

```powershell
if (-not (Test-Path compose/.env)) {
    Copy-Item compose/.env.example compose/.env
}
docker compose --project-directory compose -f compose/compose.yaml config --quiet
docker compose --project-directory compose -f compose/compose.yaml pull
```

`.env.example`의 이미지 태그는 현재 프로필의 예시다. `latest` 태그의 내용은 바뀔 수
있으므로 실제 배포의 image ID/digest를 별도 기록한다. 이 정리 과정에서는 이미지를
내려받거나 운영 컨테이너를 재시작하지 않았다.

## 3. 줄 인식 모델 준비

다음 공식 배포본에서 추론용 파일을 받아 해당 폴더에 둔다. 다운로드 시 선택한 revision도
기록한다. 모델은 소스 저장소나 공개 PR에 추가하지 않는다.

| 폴더 | 공식 모델 |
|---|---|
| `models/official_models/PP-OCRv5_mobile_rec/` | [기본 인식기](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_rec/tree/main) |
| `models/official_models/korean_PP-OCRv5_mobile_rec/` | [한국어 인식기](https://huggingface.co/PaddlePaddle/korean_PP-OCRv5_mobile_rec/tree/main) |

현재 사용한 각 배포본에는 `inference.json`, `inference.pdiparams`, `inference.yml`이 있다.
파일 이름만 맞춘 다른 모델로 바꾸지 않는다. 문단 인식은 Compose의
`PaddleOCR-VL-1.6-0.9B` 서버가 담당하고 offline 이미지의 준비된 모델을 사용한다.

## 4. 서비스 확인과 첫 PDF

아래는 최초 준비 또는 중단된 환경의 시작 명령이다. 이미 OCR이 실행 중이라면
`up`, 이미지 교체, 재시작을 별도로 실행하지 않는다.

```powershell
docker compose --project-directory compose -f compose/compose.yaml up -d
Invoke-RestMethod http://127.0.0.1:8080/health
Invoke-RestMethod http://127.0.0.1:8081/health
```

8081 응답의 `models`에서 한국어/기본 인식기를 확인한다. 포트의 노출 범위와 인증 부재는
[SECURITY](../SECURITY.md)에 설명한다. 서비스가 정상일 때 자신이 처리할 권한이 있는
작은 스캔 PDF를 `input/book.pdf`에 준비한다.

```powershell
python run.py "input/book.pdf" --out "output/book"
python run.py audit "output/book"
python run.py export "output/book/.ocr/book_line_ocr.sqlite3" "output/book/exported"
```

`book_auto_report.md`와 JSON의 검토 페이지를 확인한다. audit는 두 인식기의 일치,
원본 커버리지 등 품질 신호이며 정답률 측정은 아니다. 검색·복사·읽기 순서를 PDF 뷰어에서도
확인한다. `--fast`는 자동 최종 검증을 생략하므로 검증된 결과가 필요한 첫 실행에 쓰지 않는다.

## 문제 해결

| 증상 | 확인할 사항 |
|---|---|
| `ModuleNotFoundError` | 실행 Python과 pip 설치 환경이 같은지 확인하고 위 가상환경 경로로 실행 |
| 이미지가 없거나 서비스 시작 실패 | Docker GPU 환경, `.env`, 이미지 태그와 준비 여부 확인. CLI는 이미지를 자동 pull하지 않음 |
| 한국어 줄 모델 실패 | 두 모델 폴더와 health 응답 확인. 빈 마운트 경로를 모델 설치 완료로 보지 않음 |
| 메모리 압박 | `--cpu-workers 2 --cpu-memory-mb 2048 --verify-workers 1` 등으로 호스트 준비량부터 줄임 |
| 캐시 불일치 | 원본/API 식별자가 다르면 보존 후 별도 캐시 사용. 억지로 `--trust-cache`하지 않음 |
| 검증 실패 | 실패 보고서와 `.partial.pdf` 확인. 기존 완성본과 캐시는 보존하고 같은 명령으로 재시도 |
| 파일이 이미 처리 중 | 같은 출력의 작업이 끝나길 기다림. 실행 중인 잠금이나 DB를 삭제하지 않음 |
| 지원하지 않는 글리프 | 누락 코드포인트와 로컬 폰트 확인. 임의 문자 대체로 성공 처리하지 않음 |

전체 옵션은 `python run.py input/book.pdf --help`와 [사용 안내](OCR_USAGE.md)를 참고한다.
