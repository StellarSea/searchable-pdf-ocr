# 문서 색인

처음 사용하는 경우 [설치](SETUP.md) → [사용 안내](OCR_USAGE.md) → [책갈피](BOOKMARKS.md)
순서로 읽는다. 코드 변경은 [ARCHITECTURE](ARCHITECTURE.md)와
[기여 안내](../CONTRIBUTING.md)부터 시작한다.

## 사용과 공개

| 문서 | 내용 |
|---|---|
| [SETUP](SETUP.md) | 호스트 설치, Docker/모델 준비, 첫 실행, 문제 해결 |
| [OCR_USAGE](OCR_USAGE.md) | 모드, 재개, 보고서, 수동 검토, 폴더 처리 |
| [BOOKMARKS](BOOKMARKS.md) | 인쇄 목차 대조와 기존 PDF 책갈피 갱신 |
| [SCANTAILOR_WORKFLOW](SCANTAILOR_WORKFLOW.md) | ScanTailor에서 TIFF·PDF·OCR까지 |
| [TIFF_DPI](TIFF_DPI.md) | 페이지 물리 크기, DPI와 병렬 변환 |
| [DEPENDENCIES](DEPENDENCIES.md) | 패키지·모델 라이선스와 확인 범위 |
| [PUBLIC_RELEASE](PUBLIC_RELEASE.md) | 공개 파일 범위와 검증 결과 |
| [OpenAPI 스냅샷](openapi.json) | 당시 문단 OCR API 참고; 현재 서버의 계약 보증은 아님 |

## 구조와 구현

| 영역 | 문서 |
|---|---|
| 소유권·좌표·캐시·검증 | [ARCHITECTURE](ARCHITECTURE.md), [LOSSLESS_OPTIMIZATION](LOSSLESS_OPTIMIZATION.md) |
| 호스트 CPU 준비 | [CPU_PREPARATION](CPU_PREPARATION.md), [REPAIR_CPU_PREPARATION](REPAIR_CPU_PREPARATION.md) |
| HTTP와 GPU 공급 | [PREFETCH_PIPELINE](PREFETCH_PIPELINE.md), [DOCUMENT_PREFETCH](DOCUMENT_PREFETCH.md), [GPU_PREFETCH](GPU_PREFETCH.md) |
| 누락·경계 보완 | [REPAIR_PREFETCH](REPAIR_PREFETCH.md), [BOUNDARY_PREFETCH](BOUNDARY_PREFETCH.md) |
| 최종 검증 | [PARALLEL_VERIFICATION](PARALLEL_VERIFICATION.md) |
| 서버 PDF 준비 | [SERVER_PREPARATION](SERVER_PREPARATION.md), [SERVER_PDF_POOL](SERVER_PDF_POOL.md), [PDF_WINDOW_MEMORY](PDF_WINDOW_MEMORY.md) |
| 문자·픽셀 처리 | [TEXT_PRESERVATION_PERFORMANCE](TEXT_PRESERVATION_PERFORMANCE.md), [SUPPLEMENTARY_UNICODE_FIX](SUPPLEMENTARY_UNICODE_FIX.md), [RULE_RUN_OPTIMIZATION](RULE_RUN_OPTIMIZATION.md) |

## 실험과 운영 기록

아래 문서는 **당시 특정 입력·하드웨어·코드의 관찰 기록**이다. 새 버전의 성능 보장,
전체 책의 ground truth 또는 모든 GPU의 권장 설정으로 확대 해석하지 않는다.
문서에서 언급하는 `tmp/`, `reviews/`, `input/`, `output/`과 문서별 fixture는 로컬 자료이며
공개 저장소에 없다. 외부 사용자는 자신의 입력과 원본 대조 자료로 재현해야 한다.

- [CPU 작업자 비교](CPU_WORKER_BENCHMARK.md), [GPU 작업자 비교](GPU_WORKER_BENCHMARK.md)
- [통합 성능](INTEGRATED_PERFORMANCE.md), [자원 활용](PERFORMANCE_MAX.md)
- [GPU 경합](GPU_CONTENTION.md), [VRAM 예약 실험](VRAM_RESERVATION_EXPERIMENT.md)
- [본문 반복성](DOCUMENT_REPEATABILITY.md), [보완 CPU 풀 실험](REPAIR_CPU_POOL_EXPERIMENT.md)
- [전체 재생성 기록](FULL_REBUILD_20260914.md), [문단 복구 사례](COMPUTER_ARCHITECTURE_RECOVERY.md)
- [책갈피 검증 기록](BOOKMARKS_VALIDATION_20260916.md), [네 문서 검증 기록](FOUR_BOOKS_VALIDATION_20260920.md)
