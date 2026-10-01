# Searchable PDF OCR

스캔 PDF의 원본 화면을 유지하면서 검색·복사할 수 있는 투명 텍스트를 추가하는 로컬 OCR 도구입니다. 한국어·영문 혼용 문서, 중단 후 재개, 원본과 결과의 검증을 중심으로 설계했습니다.

Local scanned-PDF OCR with Korean/English line alignment, resumable caches, and pixel/text verification. The tested deployment uses Windows and NVIDIA Blackwell; the regression suite runs without an OCR service or GPU.

## 하는 일

- PaddleOCR-VL 문단 인식을 바탕으로 원본에 투명 텍스트를 삽입합니다.
- 한국어/기본 줄 인식기로 줄 경계를 정렬합니다. 줄 OCR 결과로 문단 원문을 대체하지 않습니다.
- 잘린 블록과 누락 의심 영역을 보완하고, 확정하기 어려운 결과는 검토 경고로 남깁니다.
- 원본과 설정에 연결된 캐시로 재개합니다. PDF·SQLite 쓰기는 한 소유자가 담당합니다.
- 기본 자동 모드는 페이지·문자·투명 텍스트·렌더링 픽셀 검증 후 최종 PDF를 게시합니다.
- 선택적으로 인쇄 목차를 본문 제목과 대조해 PDF 책갈피를 만듭니다.

**검증 통과는 OCR 정답률 100%를 의미하지 않습니다.** 픽셀 검증은 지정 렌더링 조건에서의 동일성 검사이며 원본과 파일 바이트가 같다는 뜻도 아닙니다. 복잡한 표·수식, 부분적으로 기존 텍스트가 있는 페이지, 희귀 문자는 원본 대조가 필요할 수 있습니다.

## 시작하기

확인한 호스트 환경은 **Windows, Python 3.13.15**입니다. Docker 설정은 **RTX 5080 / sm120** 프로필이며 다른 GPU의 호환성이나 최소 RAM/VRAM을 보증하지 않습니다. 원본 스캔·모델·OCR 결과는 저장소에 포함하지 않습니다.

먼저 GPU 없이 설치와 코드 검증을 할 수 있습니다. PowerShell에서 저장소 루트 기준:

```powershell
git clone https://github.com/StellarSea/searchable-pdf-ocr.git
cd searchable-pdf-ocr
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints-tested.txt
.\.venv\Scripts\python.exe run.py doctor
.\.venv\Scripts\python.exe tools/check_architecture.py
.\.venv\Scripts\python.exe run.py test
```

실제 OCR에는 별도 Docker 이미지와 모델 준비가 필요합니다. **[설치 안내](docs/SETUP.md)**의 환경·모델·서비스 확인을 마친 뒤 실행하세요. 이하 `python`은 의존성을 설치한 가상환경의 Python을 뜻합니다.

`python run.py doctor --services`는 모델·Docker·서비스를 읽기 전용으로 점검합니다.
`--json`으로 진단 결과를 저장할 수 있습니다. 이 명령은 서비스를 시작하거나 OCR을 요청하지 않습니다.

```powershell
python run.py "input/book.pdf" --out "output/book"
python run.py input --recursive --out output
python run.py "input/book.pdf" --out "output/book" --bookmarks
python run.py audit "output/book"
```

중단되면 같은 명령으로 재개합니다. 서비스가 응답하지 않으면 CLI가 로컬 Docker를 시작하거나 해당 서비스를 복구할 수 있습니다. 자세한 동작은 [운영 안내](docs/OCR_USAGE.md)를 확인하세요.

## 결과와 캐시

`--out`을 생략하면 원본 옆 `ocr_output/`을 사용합니다.

```text
output/book/
├── book_auto_searchable.pdf
└── .ocr/
    ├── book_line_ocr.sqlite3
    └── book.lock
```

본문 Markdown, JSON 보고서, 상태, 경계 체크포인트는 SQLite의 `artifacts` 테이블에 저장합니다. 필요할 때 파일로 내보낼 수 있습니다.

```powershell
python run.py export "output/book/.ocr/book_line_ocr.sqlite3" "output/book/exported"
```

`compact`와 다음 OCR 실행은 구형 JSON/MD를 DB에 통합하고 무결성·바이트 검증 후 해당 중복 파일을 제거합니다. 캐시 DB는 재개와 감사에 쓰는 사용자 데이터이므로 보존하세요. 검증 실패 시 기존 완성 PDF는 유지하며 후보는 `.partial.pdf`로 남깁니다.

## TIFF 스캔에서 시작하기

```powershell
python tools/tif_to_pdf.py "scans/book/processed" "input/book.pdf" --lossless
python run.py "input/book.pdf" --out "output/book"
```

TIFF의 가로·세로 DPI로 페이지 물리 크기를 계산하고 원래 프레임 순서로 조립합니다. 태그 처리와 병렬화 옵션은 [TIFF DPI](docs/TIFF_DPI.md), ScanTailor 설정은 [전체 스캔 작업 순서](docs/SCANTAILOR_WORKFLOW.md)를 참고하세요.

## 문서와 개발

| 목적 | 문서 |
|---|---|
| 처음 설치 / 문제 해결 | [SETUP](docs/SETUP.md) |
| 명령, 모드, 캐시, 검토 | [사용 안내](docs/OCR_USAGE.md) |
| 기여와 오프라인 검증 | [CONTRIBUTING](CONTRIBUTING.md) |
| 성능·구조 등 전체 안내 | [문서 색인](docs/README.md) |
| 변경 이력 | [CHANGELOG](CHANGELOG.md) |

코드는 `compose/`, 오프라인 회귀 테스트는 `tests/`, 수동 검증·벤치마크 도구는 `tools/`에 있습니다. `tools/`의 OCR 벤치마크는 실제 서비스를 호출할 수 있으며 자동 테스트와 구분됩니다. GitHub Actions는 Windows/Python 3.13에서 오프라인 검사만 실행하도록 구성했습니다.

## 라이선스

Copyright (c) 2026 Searchable PDF OCR contributors.

별도 고지가 없는 프로젝트 자체 코드와 문서는 **GNU Affero General Public License v3.0**
([AGPL-3.0-only](LICENSE))으로 제공합니다. 이 프로젝트의 허가는 버전 3에 적용됩니다.
라이선스의 조건에 따라 사용·수정·배포할 수 있으며, 어떠한 보증도 제공하지 않습니다.

제3자 라이브러리·모델·폰트·입력 문서에는 각자의 권리와 배포 조건이 적용됩니다.
의존성인 PyMuPDF의 AGPL/상용 조건과 패키지별 근거는
[의존성 라이선스 조사](docs/DEPENDENCIES.md)를 확인하세요.
