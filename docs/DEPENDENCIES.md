# 의존성·모델·폰트의 라이선스

직접 패키지의 라이선스 조사 기준일은 2026-09-22(Windows / Python 3.13.15),
공식 줄 모델 revision·로컬 파일 대조일은 2026-10-01이다.
프로젝트 자체 코드와 문서는 [AGPL-3.0-only](../LICENSE)로 제공한다.
라이선스 원문은 [SPDX의 AGPL v3 원문](https://raw.githubusercontent.com/spdx/license-list-data/main/text/AGPL-3.0-only.txt)을 수록했다.
이 안내는 각 배포물의 LICENSE/NOTICE를 대체하지 않는다.

## 호스트·오프라인 테스트

직접 의존성은 `requirements.txt`·`requirements-dev.txt`, 검증한 직접/전이 버전은
[constraints-tested.txt](../constraints-tested.txt)에 모았다. 개발 의존성은 모의 HTTP 서비스 시험에
쓰이며 호스트 OCR에 GPU 패키지를 설치하지 않는다.

| 패키지 | 확인된 라이선스 | 역할·공식 근거 |
|---|---|---|
| PyMuPDF / MuPDF | AGPL v3 또는 Artifex 상용 | [PDF 읽기·렌더링·삽입·검증](https://github.com/pymupdf/PyMuPDF/blob/main/COPYING), [공식 안내](https://pymupdf.readthedocs.io/en/latest/faq/index.html) |
| requests | Apache-2.0 | [로컬 HTTP API](https://github.com/psf/requests/blob/main/LICENSE) |
| pypdf | BSD-3-Clause | [독립 문자 추출 검사](https://github.com/py-pdf/pypdf/blob/main/LICENSE) |
| NumPy | BSD-3-Clause; wheel 부속 고지 별도 | [픽셀 배열](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| Pillow | MIT-CMU; 포함 코덱 고지 별도 | [이미지 처리](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| psutil | BSD-3-Clause | [자원·메모리 확인](https://github.com/giampaolo/psutil/blob/master/LICENSE) |
| FastAPI | MIT | [줄 서비스·모의 시험](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Pydantic | MIT | [API 요청 모델](https://github.com/pydantic/pydantic/blob/main/LICENSE) |

PyMuPDF는 호스트의 직접 의존성이다. OCR 모델을 별도 서버로 둬도 이 의존성이 사라지지는 않는다.
프로젝트 코드의 허가는 AGPL 버전 3에 적용되며, 제3자 구성요소를 재허가하지 않는다.
배포 시 소스 제공·고지 조건, 수정 버전을 네트워크 서비스로 제공할 때의 조건은 해당 원문을 확인한다.

기본 extra 없는 호스트 설치 메타데이터에서 확인한 전이 라이선스는 다음과 같다.
다른 OS·Python·extra·버전에서는 목록이 달라질 수 있다.

- MIT: charset-normalizer, urllib3, anyio, annotated-types, pydantic_core, typing-inspection, annotated-doc.
- BSD-3-Clause: idna, starlette. PSF-2.0: typing_extensions.
- certifi: [MPL-2.0](https://github.com/certifi/python-certifi/blob/master/LICENSE). 파일 단위 조건을 보존한다.

wheel·실행 파일·컨테이너를 재배포할 때는 포함된 폰트·코덱·BLAS 등의 부속 고지도 확인한다.

## OCR 서버와 모델

| 구성요소 | 공식 표시 | 근거 |
|---|---|---|
| PaddleOCR | Apache-2.0 | [저장소](https://github.com/PaddlePaddle/PaddleOCR) |
| PaddlePaddle | Apache-2.0 | [LICENSE](https://github.com/PaddlePaddle/Paddle/blob/develop/LICENSE) |
| PaddleX | Apache-2.0 | [LICENSE](https://github.com/PaddlePaddle/PaddleX/blob/develop/LICENSE) |
| vLLM | Apache-2.0 | [LICENSE](https://github.com/vllm-project/vllm/blob/main/LICENSE) |
| Uvicorn | BSD-3-Clause | [줄 API 서버 LICENSE](https://github.com/encode/uvicorn/blob/main/LICENSE.md) |
| pypdfium2 | Apache-2.0 / BSD-3-Clause | [라이선스 안내](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing) |
| PaddleOCR-VL-1.6 | Apache-2.0 | [공식 모델 카드](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) |
| PP-OCRv5_mobile_rec | Apache-2.0 | [확인한 revision](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_rec/tree/682f20538d8c086cb2128e5cfac775e6c4904e85) |
| korean_PP-OCRv5_mobile_rec | Apache-2.0 | [확인한 revision](https://huggingface.co/PaddlePaddle/korean_PP-OCRv5_mobile_rec/tree/c02ecaf1f22bfd1c618cce154fd19185b47e663a) |

모델 다운로드·SHA-256 확인과 이미지 digest 기록은 [설치 안내](SETUP.md)에 있다.
두 줄 모델의 로컬 세 추론 파일은 해당 공식 revision과 대조했다.
Compose의 `latest-nvidia-gpu-sm120-offline` 로컬 이미지 registry digest는 2026-10-01에 확인했고
예제 설정에 고정했다. 위 표는 상위 공식 라이선스 조사이며
전체 이미지의 Python·OS·CUDA SBOM 감사가 아니다. PDFium 등 바이너리 부속 고지도 필요하다.
이미지·모델은 소스 저장소에 포함하지 않는다.

## 폰트·입력 자료와 유지 관리

Windows 맑은 고딕·굴림 및 대체 폰트는 OS 설치본을 참조한다. 이 저장소는 그 파일의 재배포 권리를
부여하지 않으며, 생성 PDF의 폰트 포함 조건도 사용한 폰트별로 확인한다.
PyMuPDF 내장 폰트는 해당 배포물의 고지를 따른다.

책 스캔·인식 원문·수동 교정·모델 가중치는 코드 라이선스와 별개다.
`input/`, `output/`, `scans/`, `models/`, `reviews/`, 캐시·문서별 실험 fixture는 공개 대상에서
제외한다. 회귀 시험은 임시 PDF와 작은 시험 문자열을 사용한다.

의존성 업데이트 시 설치 메타데이터, 배포물의 LICENSE/NOTICE 및 오프라인 시험을 다시 확인한다.
검증 버전과 이 문서의 확인일도 갱신하고, GPU 환경에 호스트 constraints를 덮어쓰지 않는다.
