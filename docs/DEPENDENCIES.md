# 의존성 및 라이선스 조사

확인일: 2026-09-22. 호스트는 Python 3.13.15 / Windows다. 아래 버전은 현재 설치된
패키지의 `importlib.metadata`로 확인했고, 직접 의존성은 공식 라이선스와 대조했다.
이 문서는 프로젝트 자체의 사용 허가를 부여하는 LICENSE 파일이 아니다.

## 먼저 결정할 사항

**PyMuPDF가 전체 배포 조건을 결정하는 핵심 의존성이다.** PyMuPDF와 MuPDF는
GNU AGPL v3 또는 Artifex 상용 라이선스로 제공된다.
[공식 안내](https://pymupdf.readthedocs.io/en/latest/faq/index.html),
[라이선스 원문](https://github.com/pymupdf/PyMuPDF/blob/main/COPYING).

현재 코드에서 PyMuPDF는 PDF 읽기·렌더링·글자 삽입·저장·검증에 직접 사용된다.
별도 OCR 서버로 분리해 놓았다는 이유로 이 Python 프로그램의 PyMuPDF 의존성이 없어지지 않는다.
자체 작성 코드에 MIT나 Apache-2.0을 붙여도 PyMuPDF까지 그 조건으로 재허가할 수는 없다.
현재 구성을 공개 소스로 배포하려면 AGPL-3.0을 프로젝트 라이선스로 채택하는 방향이
가장 명확하다. 폐쇄형 배포를 원하면 상용 계약이나 PDF 계층 교체를 별도로 검토해야 한다.
이는 확인된 의존성을 바탕으로 한 배포 방향 제안이며, 아직 프로젝트 라이선스를 선택한 상태는 아니다.

AGPL은 상업적 사용을 금지하는 라이선스가 아니다. 적용되는 결합물의 배포 시 소스 제공과
동일한 자유를 보장하는 조건이 있으며, 수정 버전을 네트워크 서비스로 제공할 때의
소스 제공 조건도 확인해야 한다. 단순히 GitHub에 코드 일부를 올렸다는 사실만으로
모든 배포 방식의 조건을 충족하는 것은 아니다.

## 호스트 및 오프라인 테스트의 직접 의존성

### 자체 코드의 라이선스 선택지

| 선택 | 주요 특성 | 현재 구성에서의 의미 |
|---|---|---|
| AGPL-3.0 | 상업적 사용 허용, 배포 시 소스 제공, 수정 버전의 네트워크 서비스 소스 제공 조건 | PyMuPDF를 유지한 공개 배포의 명확한 선택 |
| GPL-3.0 | 배포 시 소스 제공, AGPL의 네트워크 조항과 구분 | AGPL 구성요소와 결합한 부분의 AGPL 의무를 없애지 않음 |
| MIT | 짧은 허가문, 저작권/허가 고지 유지, 폭넓은 재사용 | 자체 코드에는 선택 가능. 전체 프로그램이 MIT 조건만 따르는 것은 아님 |
| Apache-2.0 | 명시적 특허 허여·종료, 변경 표시와 NOTICE 조건 | 자체 코드에는 선택 가능. PyMuPDF 조건은 별도로 남음 |
| BSD-3-Clause | 폭넓은 재사용, 저작권자/기여자 이름의 홍보 사용 제한 | 자체 코드의 선택지이며 AGPL 의존성을 해소하지 않음 |

MIT/Apache/BSD 코드를 AGPL 의존성과 함께 사용하는 것과, 결합 프로그램 전체를
허용적 조건만으로 재배포하는 것은 다르다. 비공개 상용 제품에 포함할 자유를 제공하려면
PyMuPDF 상용 계약의 배포 범위를 확인하거나 PDF 계층을 교체해야 한다.
라이선스 이름만 바꿔 이 조건을 해소할 수는 없다. 라이선스 선택은 사용자가 검토 중이다.

각 허가문 근거는 아래 직접 의존성의 MIT, Apache, BSD, AGPL 공식 원문을 참고한다.
이 표는 배포 선택을 설명하는 요약이며 원문을 대체하지 않는다.

### 설치된 패키지

| 패키지 | 설치 버전 | 확인된 라이선스 | 역할 / 공식 원문 |
|---|---|---|---|
| PyMuPDF | 1.28.2 | AGPL-3.0 또는 상용 | [PDF 처리](https://github.com/pymupdf/PyMuPDF/blob/main/COPYING) |
| requests | 2.33.1 | Apache-2.0 | [로컬 HTTP API](https://github.com/psf/requests/blob/main/LICENSE) |
| pypdf | 6.16.2 | BSD-3-Clause | [독립 문자 추출 검사](https://github.com/py-pdf/pypdf/blob/main/LICENSE) |
| NumPy | 2.3.4 | BSD-3-Clause; wheel 내 부속 라이선스 별도 | [픽셀 배열 연산](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| Pillow | 11.3.0 | MIT-CMU; 포함 코덱 조건 별도 | [이미지 처리](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| psutil | 7.1.0 | BSD-3-Clause | [자원/메모리 확인](https://github.com/giampaolo/psutil/blob/master/LICENSE) |
| FastAPI | 0.135.3 | MIT | [줄 OCR 서비스 및 모의 테스트](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Pydantic | 2.12.5 | MIT | [API 요청 모델](https://github.com/pydantic/pydantic/blob/main/LICENSE) |

표의 라이선스는 각 프로젝트의 주 라이선스다. wheel·실행 파일·컨테이너를 묶어
재배포하면 그 안의 폰트, 코덱, BLAS 등 부속 구성요소의 고지도 함께 확인한다.

## 현재 호스트에서 확인한 전이 의존성

기본 extra 없이 위 패키지의 의존성 메타데이터를 재귀적으로 읽은 결과다.
다른 OS, Python, extra 또는 업데이트에서는 목록이 달라진다.

| 패키지 | 버전 | 설치 메타데이터의 라이선스 |
|---|---|---|
| charset-normalizer | 3.4.7 | MIT |
| idna | 3.11 | BSD-3-Clause |
| urllib3 | 2.6.3 | MIT |
| certifi | 2026.2.25 | MPL-2.0 |
| starlette | 1.0.0 | BSD-3-Clause |
| anyio | 4.13.0 | MIT |
| annotated-types | 0.7.0 | MIT |
| pydantic_core | 2.41.5 | MIT |
| typing_extensions | 4.15.0 | PSF-2.0 |
| typing-inspection | 0.4.2 | MIT |
| annotated-doc | 0.0.4 | MIT |

certifi는 단순 MIT/BSD 묶음으로 표기하면 안 된다.
[공식 LICENSE](https://github.com/certifi/python-certifi/blob/master/LICENSE)는 MPL-2.0이다.
이 파일 단위 조건을 보존해야 하며, 이것만으로 전체 자체 코드를 MPL로 바꿀 필요가 있다는 뜻은 아니다.

## OCR 서버와 모델

| 구성요소 | 공식 표시 | 근거 |
|---|---|---|
| PaddleOCR | Apache-2.0 | [저장소](https://github.com/PaddlePaddle/PaddleOCR) |
| PaddlePaddle | Apache-2.0 | [LICENSE](https://github.com/PaddlePaddle/Paddle/blob/develop/LICENSE) |
| PaddleX | Apache-2.0 | [LICENSE](https://github.com/PaddlePaddle/PaddleX/blob/develop/LICENSE) |
| vLLM | Apache-2.0 | [LICENSE](https://github.com/vllm-project/vllm/blob/main/LICENSE) |
| Uvicorn | BSD-3-Clause | [줄 API 서버 LICENSE](https://github.com/encode/uvicorn/blob/main/LICENSE.md) |
| pypdfium2 | Apache-2.0 / BSD-3-Clause | [서버 PDF 디코더 라이선스 안내](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing) |
| PaddleOCR-VL-1.6 | Apache-2.0 | [공식 모델 카드](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) |
| PP-OCRv5_mobile_rec | Apache-2.0 | [공식 모델 카드](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_rec) |
| korean_PP-OCRv5_mobile_rec | Apache-2.0 | [공식 모델 카드](https://huggingface.co/PaddlePaddle/korean_PP-OCRv5_mobile_rec) |

현재 Compose는 `latest-nvidia-gpu-sm120-offline` 태그의 상위 이미지를 참조한다.
서버 측 Uvicorn과 pypdfium2도 코드의 직접 import 목록에서 확인했다. 위 라이선스는
공식 상위 안내이며 실제 컨테이너 내부 설치 버전은 이번 조사에서 수집하지 않았다.
pypdfium2에 포함된 PDFium은 BSD 계열이며 바이너리의 부속 구성요소 고지도 필요하다.
위 표는 상위 프로젝트/모델의 공식 라이선스 확인이며, 실행 중인 이미지 전체의 SBOM이나
그 안의 모든 Python·OS·CUDA 패키지를 감사한 결과는 아니다. 다운로드한 모델 바이트와
모델 카드 revision의 일치도 이번 조사에서 검증하지 않았다. 이미지/모델을 저장소에
포함하지 않으며, 향후 바이너리 배포 시에는 실제 digest와 구성요소별 고지를 수집해야 한다.

## 폰트와 입력 문서

Windows의 맑은 고딕·굴림 및 대체 폰트는 OS 설치본을 참조한다. 그 폰트 파일을
프로젝트와 함께 재배포할 권리를 이 저장소가 부여하지 않는다. 생성 PDF의 폰트 포함 조건도
사용한 폰트별로 확인한다. PyMuPDF 내장 폰트는 해당 배포물의 고지를 따른다.

책 스캔, 인식 원문, 수동 교정 파일, 모델 가중치는 코드 라이선스와 별개다.
`input/`, `output/`, `scans/`, `models/`, `reviews/`, 문서별 실험 fixture는 공개 대상에서
제외한다. 회귀 테스트는 임시 PDF와 작은 시험 문자열을 사용한다.

## 유지 관리

`requirements.txt`와 `requirements-dev.txt`는 직접 의존성을 선언한다.
`constraints-tested.txt`는 위 호스트에서 확인한 직접/전이 패키지 버전을 기록한다.
GPU 환경을 이 호스트 constraints로 덮어쓰지 않는다. 의존성 업데이트 때 설치 메타데이터,
각 배포물의 LICENSE/NOTICE 및 오프라인 테스트를 다시 확인하고 이 문서의 확인일을 갱신한다.
