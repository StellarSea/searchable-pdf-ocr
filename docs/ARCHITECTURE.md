# OCR 구조와 변경 규칙

이 문서는 개발자와 AI가 변경할 위치, 데이터의 의미, 보존해야 할 동작을 빠르게 찾기 위한 지도다.
사용자는 계속 `python run.py ...`를 사용한다. 내부 모듈 분리는 명령·모델·출력 형식의 변경이 아니다.

## 파일별 책임

| 파일 | 책임 | 하지 않는 일 |
|---|---|---|
| `run.py` | PDF·폴더·audit·test·organize 명령 분배 | 모델 실행, PDF 알고리즘 구현 |
| `compose/ocr_to_searchable_pdf.py` | 작업 흐름, 줄 인식 조정, overlay, 검증 후 게시, 과거 import 호환 | 순수 알고리즘의 중복 구현 |
| `compose/ocr_api.py` | HTTP 요청·재시도·응답 수 확인·모델/캐시 식별자 | Docker 시작, 파일 저장 |
| `compose/ocr_source.py` | PDF 분할·회전 정규화·원본 SHA-256 | 원본 PDF 수정 |
| `compose/ocr_document_prefetch.py` | 본문 HTTP 2묶음 선행 요청·순서 보존·입장 예산·순차 복구 | PDF/SQLite 스레드 공유·모델/배치 경계 변경 |
| `compose/ocr_repair_prefetch.py` | 유실/경계 보완 OCR 1요청과 다음 블록 CPU 준비 겹치기·소유 스레드 판정 | PDF 접근·모델/캐시 변경·병렬 GPU 요청 |
| `compose/ocr_repair_prepare.py` | 유실 보완의 원본 줄 탐지·읽기 전용 CPU 풀·메모리 입장·위치 보존 순차 복구 | OCR·PDF/SQLite 쓰기·모델/임계값 변경 |
| `compose/ocr_schema.py` | 다양한 OCR JSON의 글자·박스·크기 추출, 수동 검토 적용 | 모델 호출 |
| `compose/ocr_bookmarks.py` | 인쇄 목차·본문 제목 대조, 책갈피 계획·소유 스레드 삽입·저장 후 목적지 검증 | OCR 호출·원시 캐시 수정·임의 사용자 책갈피 교체 |
| `compose/ocr_bookmark_layout.py` | 목차 행·독립 장 제목·분리 번호/제목·장 소개·인쇄 쪽수 해석 | PDF/OCR/캐시 변경 |
| `compose/ocr_bookmark_match.py` | 인쇄 쪽수·제목·장 범위에 따른 목적지 계획과 보류 근거 | 본문 수정·근거 없는 이동 위치 생성 |
| `compose/ocr_bookmark_refresh.py` | 원본/레이아웃 식별 검증, 책갈피만 증분 갱신, 전 페이지 보존 검사·백업·게시 | OCR 호출·임의 사용자 책갈피 교체·원시 캐시 변경 |
| `compose/ocr_text.py` | HTML/수식 정리·한글 줄 나눔·문자 보존 검사·경계 정렬 | PDF/SQLite 쓰기 |
| `compose/ocr_render.py` | 동일 픽셀 렌더링·줄/표 탐지·페이지 스케일 보정 | OCR 텍스트 수정 |
| `compose/ocr_layout.py` | 부모/CPU 작업자의 공통 블록 배치·좌표/이미지 설정 | 프로세스·캐시 쓰기 |
| `compose/ocr_prepare.py` | 캐시 우선 계획·제한된 CPU 풀·메모리 예산·순차 복구 | GPU 호출·PDF/SQLite 작업자 쓰기 |
| `compose/ocr_gpu_prefetch.py` | 준비된 PNG의 언어별 요청 묶기·제한된 선행 큐·응답 캐시 저장 | 모델 생성·PDF/폰트 작업·감사 결정 게시 |
| `compose/ocr_fonts.py` | 글리프 대체 폰트·투명 텍스트 배치 | OCR 인식 판단 |
| `compose/ocr_storage.py` | 원자적 JSON 저장, `LineCache` 응답/탐지/게시 전 결정, `BoundaryCache` 페이지 체크포인트 | 글자/줄 선택 판단 |
| `compose/ocr_artifacts.py` | SQLite 텍스트 항목, 기존 파일 참조 해석, 검증 후 통합 삭제, 명시적 내보내기 | PDF/모델 변경, 잠금 없는 운영 자료 이관 |
| `compose/ocr_boundary.py` | 잘린 박스 확장·보완 인식·보존 검사·별도 파생 캐시 | 원시 OCR 캐시 덮어쓰기 |
| `compose/ocr_modes.py` | CLI 옵션·모드·결과/완료 상태 이름 | 작업 실행 |
| `compose/ocr_workflow.py` | 출력 폴더/잠금·서비스 준비·검증 호출 호환·요약 | 문단 텍스트 결정 |
| `compose/ocr_verify.py` | 전 페이지 픽셀/문자 검증·제한된 읽기 전용 CPU 풀·전체 순차 복구 | PDF/SQLite 쓰기·OCR 호출·검사 생략 |
| `compose/ocr_batch.py` | 파일별 순차 실행·완료 확인·재개 | 페이지 내부 알고리즘 |
| `compose/audit_ocr_quality.py` | 원본 커버리지·숫자 순서·두 모델 일치 신호 | 자동으로 정답 확정 |
| `compose/line_ocr_server.py` | GPU 모델 생명주기·줄 분할·배치 추론·HTTP 서버 | PDF 파일 접근 |
| `compose/ocr_server_pdf.py` | 서버의 PDF 변환용 제한된 spawn 풀·공유 메모리·순차 복구 | GPU 모델 생성·OCR/캐시 변경·공유 메모리 뷰 반환 |
| `compose/ocr_server_entrypoint.py` | 환경 변수로 선택하는 PaddleX reader 연결·서버 종료 시 풀 회수 | 모델/이미지 버전·인식 옵션 변경 |

`ocr_to_searchable_pdf`의 기존 함수 이름은 재노출하므로 예전 도구는 계속 import할 수 있다.
새 코드는 소유 모듈에서 직접 import한다. 예: 문자 정리는 `ocr_text.strip_html`, 렌더링은
`ocr_render.PageRaster`. 테스트에서 설정을 바꿀 때도 소유 모듈을 대상으로 한다.
호환 진입점의 상수 재할당으로 다른 모듈의 설정을 바꾸는 방식은 지원하지 않는다.

## 데이터 흐름과 좌표

1. 원본 PDF를 해시하고, 원본/API가 일치하는 문단 캐시 또는 중간 저장을 읽는다.
2. 필요한 문단만 OCR하고, 별도 파생 데이터에서 잘린 경계를 복구한다. 수동 검토는 원본과 정확히 대조한다.
3. OCR 이미지 좌표의 박스를 PDF 좌표로 변환한다. `page.rect` 기준이며 회전/cropbox 처리는 렌더링·폰트 모듈이 담당한다.
4. `PageRaster`가 overlay 삽입 전의 페이지 그리기 명령을 공유한다. 페이지 전체 비트맵을 미리 보관하지 않는다.
5. 원본 픽셀에서 줄을 찾고, 줄 OCR은 **경계 안내**에만 사용한다. 삽입 문자열은 문단 원문을 유지한다.
6. 부분 PDF를 만든 뒤 픽셀·문자 수·페이지 수 등을 검증한다. 통과한 결과만 최종 PDF와 보고서로 게시한다.

- OCR `block_bbox`: OCR 이미지의 픽셀 좌표 `[x0, y0, x1, y1]`.
- 줄 사각형: `page.rect` 좌표계의 PDF point. 페이지 번호는 외부 JSON/CLI에서 1부터 시작한다.
- 경계 복구의 내부 배열: 정규화한 분석 래스터의 픽셀 좌표. 원시 OCR 박스와 직접 혼합하지 않는다.
- `page_coordinate_scale`: 비정상적으로 큰 스캔 PDF의 물리 크기 임계값만 보정한다. 해상도를 낮추는 함수가 아니다.

TIFF 입력 PDF 생성은 별도 도구 `tools/tif_to_pdf.py`가 소유한다. 원본 프레임의
가로·세로 DPI와 물리 단위로 페이지 크기를 계산하고 JPEG에도 DPI를 전달한다.
누락/잘못된 DPI는 경고 후 기존 600 DPI 기본값을 쓴다. 기존 PDF를 재해석하거나
OCR 캐시를 변경하지 않는다. [TIFF 해상도 안내](TIFF_DPI.md)를 참고한다.
무손실 TIFF 변환은 각 이미지 삽입 직후 해당 스트림을 압축해 비압축 책 전체가 누적되는 것을 줄인다.
이미지의 실제 PDF 샘플 형식(1비트 최적화 포함)을 유지하며, PDF 접근과 저장은 한 스레드가 소유한다.
TIFF 변환 CLI는 기본 최대 4개 spawn 프로세스에서 각자 한 프레임 PDF를 준비한다.
프레임 헤더로 계산한 메모리 예산과 가용 RAM으로 작업자 수를 제한하고, 제출/완료 대기는 작업자 수 이하다.
주 프로세스만 원래 순서대로 최종 PDF를 조립·저장한다. 풀 실패는 미소비 페이지부터 순차 복구하며,
OCR 서비스·캐시와는 무관하다. `--workers 1`은 순차 경로, Python API 기본값도 순차 경로다.

## 정확도와 호환성의 기준

- 구조 변경에는 모델·입력 폭·ZOOM·검출 임계값·캐시 버전 변경을 섞지 않는다.
- 수식·HTML·표·라틴 단어·숫자·주소 보존 규칙은 `ocr_text`와 회귀 테스트가 소유한다.
- 박스 확장 후 재인식 결과가 보존 검사를 통과하지 못하면 원문을 유지하고 검토 경고를 남긴다.
- OCR 캐시의 SHA-256, PNG 해시, 원본+이미지 수동 검토 키, JSON/SQLite 이름을 유지한다.
- `LineCache`는 OCR 응답을 중간 저장하지만, 새 결정 기록은 임시 테이블에 쌓는다.
  검증된 일반 PDF를 게시한 뒤에만 `publish_decisions()`를 호출한다. 실패/디버그가 기존 감사 기록을 덮어쓰면 안 된다.
- `source-crop-v1`, `boundary-ink-v3`, 모델 캐시 이름의 과거 `w4`는 의도적으로 유지한다.
- PyMuPDF 객체를 여러 스레드에서 쓰지 않는다. PDF와 SQLite 변경은 한 소유자가 순차 수행한다.

유실 보완 CPU 입장은 작업자 기본 예약량과 실행 중 페이지별 래스터 예약량을 합산한다.
완료된 결과는 좌표만 보관하며 대기 창은 작업자 수의 2배 이하로 유지한다.
작업자별 원본 PDF 소유권과 부모의 OCR 응답·체크포인트 순서는 그대로다.
메모리 추정식·검증과 재현 자료는 `REPAIR_CPU_PREPARATION.md`를 참고한다.

## 테스트 및 수정 순서

### 선택적 책갈피 자동 생성

`--bookmarks`는 기존 OCR 레이아웃에서 목차와 본문 제목을 대조한다. 기존 `--toc`/`--toc-all`
동작은 유지하며, 함께 지정하면 `--bookmarks`가 우선한다. 기본 실행 옵션과 원시 캐시 키는
유지하고 활성화한 실행의 완료 옵션에만 책갈피 알고리즘 버전을 추가한다.
목차 계획은 원시 입력을 수정하지 않는 파생 데이터다. PDF 소유자가 새 책갈피만 삽입하고,
임시 PDF를 다시 열어 제목·계층·페이지·좌표를 검증한 후 기존 전 페이지 검증과 게시를 진행한다.
기존 책갈피가 있으면 전체를 보존하며 외부 링크를 포함한 의미가 저장 후 유지되는지 확인한다.
자동 모드의 결과는 원본 identity와 함께 기존 보고서의 `bookmarks` 항목으로 게시한다.
비자동 모드도 책갈피 검증 전에는 완성 PDF를 교체하지 않으며, 별도 `*_bookmarks.json`
DB 항목에 원본 identity와 결과를 기록한다. `tests/test_ocr_bookmarks.py`가 파싱·목적지·원본
픽셀/문자/좌표·회전/cropbox·캐시 재사용·게시 실패를 오프라인으로 검사한다.
형식 지원과 정확도 제한은 [BOOKMARKS.md](BOOKMARKS.md)를 참고한다.

v2는 장 소개의 절 목록을 본문 목적지에서 제외하고 인쇄 쪽수로 검색 범위를 제한한다.
기존 결과에는 `run.py bookmarks`로 OCR 없이 적용한다. 이전 생성 보고서와 실제 책갈피가
정확히 일치할 때만 기존 생성 항목을 교체한다. 기존 PDF/보고서는 별도 history에 보존한다.
새 결과는 모든 기존 비목차 객체·스트림, 전 페이지 문자/좌표/픽셀을 비교한 뒤 게시한다.
원본 이미지를 확인한 표시 제목·인쇄 쪽수 보정은 source/layout SHA-256으로 묶인 별도
`--bookmark-review` / `bookmarks --review` 자료에 둔다. 원시 OCR 내용과 키는 수정하지 않는다.
`tests/test_ocr_bookmark_refresh.py`가 실제 실패 형식과 갱신 실패/사용자 수정 보호를 검사한다.

### 보조 파일 통합 (사용자 요청에 따른 저장 형식 변경)

CLI는 `document_artifact()`로 JSON/MD를 같은 책의 `*_line_ocr.sqlite3`에 저장한다.
새 `artifacts(name, content, sha256)` 테이블을 추가하며 기존 줄 캐시 테이블/키는 유지한다.
`TextArtifact`는 OS 경로가 아니다. 텍스트 소비자는 `resolve_text()`/`find_artifact()`를 쓰고,
PDF는 계속 실제 경로로 처리한다. `atomic_json()`과 `write_summary()`는 DB 항목이면 트랜잭션 저장한다.
`output_lock()` 안에서 기존 텍스트를 이관하고 무결성/바이트 검증 후 해당 파일만 삭제한다.
삭제된 텍스트는 `run.py export`로 복원 가능하다. 원본·최종 PDF·모델은 이관 대상이 아니다.
운영 캐시에는 실패/완료 상태와 기존 감사/원문을 분리된 항목으로 보존한다.
새 통합 DB의 생성·충돌·잠금·내보내기·캐시 재개는 `tests/test_ocr_artifacts.py`로 검증한다.

```powershell
python -m pip install -r requirements-dev.txt
python tools/check_architecture.py
python run.py test
```

자동 테스트는 모델 생성과 HTTP 호출을 가짜 응답으로 대체하며 GPU/Docker가 필요 없다.
`check_architecture.py`는 GPU 패키지를 import하지 않고 문법과 내부 순환 의존성을 검사한다.
또한 Python 심볼 테이블로 미정의 전역 참조를 검사한다. 드문 분기에서만 쓰는 함수의
import 누락도 실행 전에 잡기 위한 검사이며, 모든 런타임 오류를 검증하는 전체 린터는 아니다.

29페이지 다중 줄 표 셀에서 드러난 `ocr_render.split_text` import 누락을 수정했다.
기존 단일 줄/가로 병합 셀 테스트에 더해 다중 물리 줄 셀 테스트를 추가했다.
이번 수정은 알고리즘/임계값을 바꾸지 않는다. 실제 29페이지의 글자·좌표·픽셀·보고서는
리팩토링 이전과 같으며, 기존 미배정 상자 경고 5개를 없앴다는 의미는 아니다.

| 수정 종류 | 우선 확인 |
|---|---|
| 문자/표/수식 | `test_ocr_regressions.py` |
| 박스 잘림 | `test_ocr_boundary.py` |
| 렌더링/색인 캐시 | `test_ocr_performance.py` |
| 재개/게시/폴더 작업 | `test_ocr_automatic.py` |
| GPU 스케줄링 | `test_line_worker_tuning.py` + 실제 표본 벤치마크 |
| 모듈/자원 생명주기 | `test_refactoring.py` |

실제 문서 비교 도구 `tools/compare_refactor.py`는 명시적으로 넘긴 이전 코드 스냅샷과
현재 구현을 비교한다. 원본/운영 캐시를 수정하지 않으며 테스트 산출물만 지정 폴더에 만든다.
같은 가짜 OCR 응답을 두 구현에 사용하므로 **리팩토링의 동작 동일성**만 검사한다.
실제 인식 정확도나 서버 속도를 평가하는 도구가 아니다.

2026-09-13 리팩토링 검증에서 기존 경계 복구가 적용된 실제 6·7·166페이지의
글자·글자 좌표·렌더링 픽셀·이미지 캐시 키·보고서가 변경 전과 동일했다.
리팩토링 전 코드와 비교 자료는 `tmp/refactor_baseline/`, `tmp/refactor_comparison/`에 보관했다.
부분 표본 결과를 전체 책의 완전한 정확도 보장으로 확대 해석하지 않는다.

## 운영 범위

이번 구조 변경은 GPU 서비스, 현재 실행 중인 OCR 작업, 원본 PDF, 기존 결과/캐시를 변경하지 않는다.
GPU 작업자 기본값 1과 입력 폭 320은 직전 벤치마크 설정을 유지한다.
자세한 운영/속도 안내는 `OCR_USAGE.md`, GPU 측정은 `GPU_WORKER_BENCHMARK.md`를 참고한다.

후속 정확도 보존 최적화는 `LOSSLESS_OPTIMIZATION.md`에 정리했다. 줄 탐지의 파생 캐시는
원본/렌더러/좌표/전체 설정/구현 해시에 묶이며 렌더 실패는 캐시하지 않는다.
경계 복구의 페이지별 저장 디렉터리와 기존 전체 JSON을 함께 관리한다.
줄 서비스의 요청 조정 작업자는 GPU 풀과 별개이며, PDF/SQLite 소유 규칙은 그대로다.

후속 선행 준비는 `PREFETCH_PIPELINE.md`를 참고한다. 자동 overlay는 HTTP만 추가 스레드로
보내고 메인 스레드에서 다음 문단 하나를 준비한다. `--no-prefetch`로 순차 실행할 수 있다.
PDF/폰트/SQLite를 HTTP 작업자에 넘기거나 무제한 futures/페이지 큐를 만들면 안 된다.

CPU 페이지 병렬화는 CLI 자동 모드에서 최대 16개(논리 CPU 수 이하)가 기본이며 `--cpu-workers 0`으로 해제한다.
부모는 CPU/HTTP 대기 중 완료 결과를 회수하고 다음 작업을 보충한다. 준비 창은 작업자 수의
2배로 제한하고, PDF 삽입과 탐지 캐시 쓰기는 기존 페이지 순서/소유 스레드를 유지한다.
`ocr_prepare.CPUPreparer`가 캐시 우선·메모리 예산·시간 초과·순차 복구를 소유한다.
`overlay(..., prepared_pages=...)` 실험 콜백도 남지만 운영 CLI는 `cpu_workers`를 전달한다.
작업자는 각각 PDF를 읽고 좌표/문자/PNG만 반환한다. 부모만 탐지 캐시와 PDF를 쓴다.
설계/복구/옵션은 `CPU_PREPARATION.md`, 이전 실측은 `CPU_WORKER_BENCHMARK.md`를 참고한다.

GPU 선행 공급은 `GPU_PREFETCH.md`를 참고한다. CPU 완료 결과의 소유 스레드 관찰 콜백을 통해
이미 준비된 PNG만 묶는다. HTTP 작업자에는 바이트/언어만 전달하고 응답과 감사 결정의 저장을 분리한다.
`--no-gpu-prefetch`로 이 기능만 해제할 수 있으며 캐시 키나 완료 식별자는 바뀌지 않는다.

본문 OCR의 요청 겹치기는 `DOCUMENT_PREFETCH.md`를 참고한다. CLI `--ocr-requests 2`가 기본이며
`1`로 순차 복구한다. 입력 분할과 체크포인트는 소유 스레드에 두고 원래 배치 경계를 유지한다.

전체 자원 활용의 진행 상태와 실측은 `PERFORMANCE_MAX.md`를 참고한다. CPU 풀은
시작 시 페이지 예약 예산에 맞는 프로세스 수만 생성해 과도한 초기화로 인한 전체 순차 복구를 줄인다.
설정 상한 `workers`와 실제 풀 크기 `pool_workers`를 구분한다. 모델/입력/캐시 키는 유지한다.

유실 보완의 같은 페이지 그리기 명령 재사용과 HTTP 겹치기는 `REPAIR_PREFETCH.md`에 정리했다.
`--no-prefetch`는 기존 line-ocr 겹치기와 이 보완 겹치기를 함께 해제한다. 원본 페이지 렌더링,
판정/중간 저장은 계속 소유 스레드에서 수행하며 HTTP 작업자는 PNG 바이트만 받는다.

최종 검증은 `PARALLEL_VERIFICATION.md`를 참고한다. 일반 PDF는 `--verify-workers 8`이 기본이며,
각 프로세스가 독립 PDF를 읽고 판정만 반환한다. 메모리/작업자 오류는 풀을 회수한 뒤 모든
페이지를 순차 재검사한다. `--verify-workers 0`으로 검증 병렬화만 해제할 수 있다.

서버 PDF 디코딩의 별도 CPU 풀은 `SERVER_PDF_POOL.md`를 참고한다. native reader를 캡처한
뒤 시작 경로에서만 교체한다. 실패한 풀을 회수하기 전에 공유 메모리를 해제하거나,
공유 메모리 뷰를 모델/HTTP 코드로 반환하지 않는다. 이미지 입력 단계에서는 풀을 회수한다.

긴 획 탐지의 정확한 Boolean 구간 처리는 `RULE_RUN_OPTIMIZATION.md`에 정리했다.
전체 이미지 int32 누적 배열 대신 행 chunk를 사용한다. 임계값/해상도는 그대로이며,
파생 탐지 캐시만 구현 해시에 따라 다시 계산한다. 비 Boolean 입력의 수치 의미는 유지한다.

유실 보완 CPU 준비의 운영 연결은 `REPAIR_CPU_PREPARATION.md`를 참고한다. 이 단계는
실측한 최대 4개를 기존 `--cpu-workers`/`--cpu-memory-mb` 이하에서 사용한다. 완료된
resume 항목은 계획도 생략한다. 작업자 결과는 원본 stamp·페이지·사각형 식별자로 검증하며,
실패하면 소유 풀만 회수하고 현재 블록부터 순차 처리한다. 이미 저장한 OCR은 반복하지 않는다.
체크포인트 실패와 사용자 중단은 CPU 복구로 삼키지 않는다. 최종 보고서에 `repair_cpu_stats`를 남긴다.

경계 보완은 `BOUNDARY_PREFETCH.md`를 참고한다. 같은 단일 HTTP 조정기를 재사용하되,
경계 캐시의 페이지 단위 완료 의미를 보존한다. 뒤 페이지 CPU 준비는 앞선 마지막 요청과
겹칠 수 있지만 앞 페이지 저장이 성공하기 전 다음 HTTP를 보내지 않는다. `--no-prefetch`로
해제하며 모델/입력/캐시 버전은 유지한다. 진단 필드는 `boundary_feed_stats`다.

문자 보존/비교 개선은 `TEXT_PRESERVATION_PERFORMANCE.md`를 참고한다. `ocr_text`는
HTML에 섞인 코드와 표 셀 entity의 한 번 해석, 숫자/주소의 순서 보존을 소유한다.
동일 문자열과 유사도 상한을 이용한 계산 생략은 기존 임계값을 바꾸지 않는다.
표 파서의 셀 문자열은 후속 `strip_html`용으로 escape된 상태이며 바로 PDF에 삽입하지 않는다.

보충 유니코드의 PDF 매핑 보정은 `SUPPLEMENTARY_UNICODE_FIX.md`를 참고한다.
`ocr_fonts`는 새로 포함한 폰트의 잘못된 scalar ToUnicode 목적값만 UTF-16으로
보정하며, 원본 xref 경계 이전의 폰트/공유 스트림을 수정하지 않는다. 이 처리는
저장 직전 PDF 소유자만 실행하고 검증 기준이나 원시 OCR 문자를 바꾸지 않는다.

문단 textbox 넘침과 누락 글리프의 예외 배치는 `ocr_fonts`가 소유한다.
기존 문단 배치를 우선하며, 실패 시 대체 폰트와 상자 내 축소 배치를 사용하고
`paragraph_compacted` 검토 경고를 남긴다. 실제 컴퓨터 구조론 7페이지의
저장 실패와 전체 복구 검증은 `COMPUTER_ARCHITECTURE_RECOVERY.md`를 참고한다.

서버 PDF 준비의 공유 출력 창은 `PDF_WINDOW_MEMORY.md`를 참고한다. 모든 페이지
출력을 동시에 할당하지 않고 완료/복사 후 즉시 해제한다. 메모리 부족 시 활성 창을
2페이지까지 줄이지만 기존 풀 전체 비용은 예약하며, 오류 시 풀 회수 후 전체 순차
복구한다. 2026-09-14 기존 OCR 종료 확인 후 API에 적용했고 실제 병렬 요청에서 새 창 통계를
확인했다. 두 책 전체 재생성의 진행/검증은 `FULL_REBUILD_20260914.md`를 참고한다.
