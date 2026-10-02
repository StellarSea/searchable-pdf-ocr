# OCR 구조와 변경 규칙

현재 코드의 소유권, 데이터 흐름, 보존 조건을 설명한다. 일반 명령은
[사용 안내](OCR_USAGE.md), 작업자·메모리·요청 설정은 [성능 설정](PERFORMANCE.md)을 참고한다.

## 파일별 책임

| 파일 | 책임 | 하지 않는 일 |
|---|---|---|
| `run.py` | PDF·폴더·audit·doctor·bookmarks·저장 관리·test 명령 분배 | 모델 실행, PDF 알고리즘 구현 |
| `compose/ocr_doctor.py` | 설치 패키지·모델 파일·Compose·서비스 health의 읽기 전용 진단 | 서비스 시작/재시작, 추론, PDF·캐시 쓰기 |
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

1. 원본 PDF의 SHA-256을 구하고 원본/API 설정이 일치하는 캐시와 체크포인트를 읽는다.
2. 본문 OCR 캐시를 재사용한다. 누락 보완은 원문·후보·채택 여부를 `repair_audit`에 남기고 본문 캐시와 함께 저장하며, 경계 보완은 별도 파생 캐시에 저장한다. 수동 검토는 원본 식별자와 대조한다.
3. OCR 이미지 좌표의 박스를 PDF 좌표로 변환한다. 회전과 cropbox는 렌더링·폰트 모듈이 담당한다.
4. `PageRaster`가 텍스트 삽입 전의 페이지 그리기 명령을 재사용한다. 전체 책의 래스터를 미리 보관하지 않는다.
5. 원본 픽셀에서 줄을 찾는다. 줄 OCR은 경계 안내에 사용하며 삽입 문자열은 문단 원문을 유지한다.
6. 기본 자동 모드는 후보 PDF의 페이지·문자·투명 텍스트·픽셀을 검증한 뒤 최종 PDF와 성공 보고서를 게시한다. 호환 모드의 검증 범위는 [사용 안내](OCR_USAGE.md)를 따른다.

- OCR `block_bbox`: OCR 이미지의 픽셀 좌표 `[x0, y0, x1, y1]`.
- 줄 사각형: `page.rect` 좌표계의 PDF point. 외부 JSON/CLI의 페이지 번호는 1부터 시작한다.
- 경계 보완 내부 배열: 정규화된 분석 래스터의 픽셀 좌표. 원시 OCR 박스와 직접 섞지 않는다.
- `page_coordinate_scale`: 큰 스캔 PDF의 물리 크기 임계값을 보정한다. 렌더 해상도를 낮추지 않는다.

## 보존 조건

- 구조 정리에는 모델·입력 폭·ZOOM·검출 임계값·캐시 namespace 변경을 섞지 않는다.
- 원본 픽셀, Unicode, 읽기 순서, 원본과 연결된 캐시·검토 식별자를 보존한다.
- 문자·수식·HTML·표의 정리는 `ocr_text`가 소유한다. 표 셀 문자열은 후속 `strip_html`용으로 escape된 상태다.
- 숫자·주소·문자 순서 보존 검사를 통과하지 못한 보완 결과는 채택하지 않고 검토 경고를 남긴다.
- `source-crop-v1`, `boundary-ink-v3`, 모델 캐시 이름의 `w4`는 호환성을 위해 유지한다.
- PyMuPDF 객체를 여러 스레드에서 쓰지 않는다. PDF·SQLite·최종 보고서 변경은 한 소유자가 순차 수행한다.
- 작업자 프로세스는 각자 PDF를 읽고 좌표·문자·PNG 또는 검증 결과를 반환한다. GPU 모델을 생성하지 않는다.
- HTTP 스레드에는 바이트와 언어 등 요청 데이터만 전달한다. PDF·폰트·SQLite 객체를 넘기지 않는다.
- 원본/운영 캐시/진행 중인 OCR을 개발 테스트 대상으로 변경하지 않는다.

## 저장과 게시

`LineCache`는 OCR 응답을 중간 저장한다. 자동 모드의 새 감사 결정은 임시 테이블에 쌓고, 검증된 일반
PDF를 게시한 뒤에만 `publish_decisions()`를 호출한다. 실패·디버그 실행은 성공 감사 기록을
덮어쓰지 않는다. 줄 탐지의 파생 캐시는 원본·렌더러·좌표·설정·구현 해시에 연결하며 렌더 실패는 캐시하지 않는다.

본문·보고서·상태·경계 체크포인트는 문서별 `*_line_ocr.sqlite3`의
`artifacts(name, content, sha256)`에 저장한다. 원래 줄 캐시 테이블과 키는 유지한다.
`TextArtifact`는 OS 경로가 아니므로 텍스트는 `resolve_text()`/`find_artifact()`로 읽고
PDF는 실제 경로로 처리한다. `atomic_json()`과 `write_summary()`는 `TextArtifact`에 대해 DB 트랜잭션으로 저장한다.

구형 JSON/MD 이관은 `output_lock()` 안에서 복사·무결성·바이트 검증을 마친 항목만
삭제한다. 원본·완성 PDF·모델은 이관 대상이 아니다. 충돌과 잠금 실패는 기존 자료를 보존한다.
`run.py export`로 파일을 복원할 수 있다. CLI·파일명·스키마 변경은 별도 migration 문서와 테스트가 필요하다.

## 글꼴과 책갈피

`ocr_fonts`는 글리프 fallback, 투명 텍스트 배치, 저장 전 Unicode 매핑 보정을 소유한다.
새로 포함한 폰트의 잘못된 보충 Unicode ToUnicode 목적값만 UTF-16으로 보정하며,
원본 xref 경계 이전의 폰트와 공유 스트림은 수정하지 않는다. 문단 배치를 우선하고, 넘침이나
누락 글리프가 있으면 상자 내 대체 배치를 시도해 `paragraph_compacted` 검토 경고를 남긴다.

책갈피 계획은 원시 레이아웃을 수정하지 않는 파생 데이터다. PDF 소유자가 삽입하고 저장 후
제목·계층·페이지·좌표를 검증한다. 기존 사용자 책갈피와 외부 링크 의미는 보존한다.
`run.py bookmarks`는 원본/레이아웃과 이전 생성 보고서를 대조하고, 기존 결과를 백업한 뒤
모든 비목차 객체·스트림 및 전 페이지 문자·좌표·픽셀을 확인한다. 사용자가 수정한 생성 항목은
덮어쓰지 않는다. 입력 형식과 교정 조건은 [책갈피 안내](BOOKMARKS.md)를 참고한다.

## TIFF 입력

`tools/tif_to_pdf.py`는 각 프레임의 가로·세로 DPI와 단위로 PDF 크기를 계산한다.
독립 작업자가 프레임 PDF를 준비하고 부모만 원래 순서로 조립·저장한다. 메모리 예산과
실제 작업자 수로 제출/대기 창을 제한하고 실패 시 미소비 페이지부터 순차 복구한다.
기존 PDF·OCR 캐시에는 관여하지 않는다. [TIFF DPI](TIFF_DPI.md)에 옵션과 해상도 규칙을 설명한다.

## 변경 후 검증

```powershell
python tools/check_architecture.py
python tools/check_publication.py --history
python run.py test
```

오프라인 회귀는 임시 입력과 모의 HTTP/모델 응답을 사용한다. 구조 검사는 문법, 정적으로
미정의된 전역 참조, 내부 import 순환을 확인하며 모든 런타임 오류를 잡는 전체 린터는 아니다.

| 변경 영역 | 우선 확인할 테스트 |
|---|---|
| 문자·표·수식·글꼴 | `test_ocr_regressions.py`, `test_text_preservation.py`, `test_font_unicode.py`, `test_paragraph_fallback.py` |
| 누락·경계 보완 | `test_ocr_boundary.py`, `test_ocr_repair_cpu.py`, `test_boundary_prefetch.py` |
| 렌더링·CPU·GPU 공급 | `test_ocr_performance.py`, `test_ocr_cpu.py`, `test_ocr_gpu_prefetch.py` |
| 재개·저장·게시 | `test_ocr_automatic.py`, `test_ocr_artifacts.py`, `test_ocr_verify.py` |
| 책갈피 | `test_ocr_bookmarks.py`, `test_ocr_bookmark_refresh.py` |
| 서버·생명주기 | `test_server_pdf.py`, `test_line_worker_tuning.py`, `test_refactoring.py` |
| TIFF | `test_tif_to_pdf.py` |

레이아웃 변경은 픽셀·문자·박스를 대조한다. `tools/compare_refactor.py`는 같은 모의 OCR
응답을 이전 스냅샷과 현재 코드에 적용하는 동작 비교 도구다. 인식 정확도나 전체 책의
정답률을 증명하지 않는다. 실제 성능 측정 조건은 [성능 설정](PERFORMANCE.md)에 설명한다.
