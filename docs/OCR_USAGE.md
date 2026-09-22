# OCR 사용 안내

설치와 모델 준비는 [설치 안내](SETUP.md)를 따른다. 아래 명령은 저장소 루트에서,
의존성을 설치한 Python으로 실행한다.

## 변환과 재개

```powershell
python run.py "input/book.pdf" --out "output/book"
python run.py input --recursive --out output
python run.py "input/book.pdf" --out "output/book" --bookmarks
```

기본 자동 모드는 문단 OCR, 누락·잘림 보완, 줄 정렬, 투명 텍스트 삽입과 최종 검증을 수행한다.
줄 OCR은 경계 안내에 사용하며 문단 원문을 대체하지 않는다. 한글과 한자·가나의 비중에 따라
한글/기본 줄 모델을 선택한다. 복잡한 표·수식 등은 대체 배치와 검토 경고를 남길 수 있다.
기존 텍스트가 있는 페이지는 보존하므로 일부에만 텍스트가 있는 스캔은 원본 대조가 필요하다.

중단되면 같은 명령으로 재개한다. 원본과 API 설정, 배치 크기가 일치하는 완료 부분을 재사용한다.
폴더 처리는 문서별로 순차 실행하며 기본적으로 페이지 수가 적은 문서부터 처리한다.
`--order name|size|pages`, `--redo`, `--stop-on-failure`로 순서·재실행·실패 처리를 조절한다.
출력 폴더와 생성된 PDF는 입력 탐색에서 제외한다. OCR 옵션은 각 문서 실행에 전달한다.
폴더 재실행은 원본·옵션이 일치하는 완료 문서를 건너뛴다. `--no-cache`나
`--repair-cache`를 지정하면 완료된 문서도 다시 처리한다.

서비스 기본 주소는 문단 `127.0.0.1:8080`, 줄 `127.0.0.1:8081`이다.
CLI는 응답하지 않는 서비스를 시작하거나 복구할 수 있다. 이미지·모델 설치는 먼저 완료해야 한다.

## 출력과 보고서

`--out`을 생략하면 원본 옆 `ocr_output/`에 저장한다.

| 항목 | 위치 또는 이름 |
|---|---|
| 일반 완성 PDF | `book_auto_searchable.pdf` |
| 문서별 캐시와 텍스트 자료 | `.ocr/book_line_ocr.sqlite3` |
| 보고서·상태 | DB의 `book_auto_report.md`, `book_auto_report.json`, `book_auto_status.json` |
| 폴더 처리 로그·상태 | `.ocr/batch_logs/`, `.ocr/batch_status.json` |

원시 레이아웃, 본문 Markdown, 보고서와 파생 체크포인트는 DB의 `artifacts` 테이블에 저장한다.
기존 파일 경로와 `DB경로::항목이름` 참조를 지원한다. 도구에서 직접 읽을 때는
[구조 안내](ARCHITECTURE.md)의 텍스트 항목 API를 사용한다.

```powershell
python run.py export "output/book/.ocr/book_line_ocr.sqlite3" "output/book/exported"
python run.py compact "output/book"
python run.py organize "output/book"
```

`export`는 자료를 파일로 내보내며 내용이 다른 기존 파일을 덮어쓰지 않는다.
`compact`와 다음 OCR 실행은 구형 JSON/MD를 잠금 안에서 DB에 통합하고,
무결성과 바이트 일치를 확인한 중복 파일만 제거한다. `organize`는 구형 보조 파일 배치를 정리한다.
원본·완성 PDF·모델은 통합 삭제 대상이 아니다. DB는 재개와 감사에 쓰므로 보존한다.

검증 실패 시 기존 완성 PDF와 성공 보고서를 유지하고 후보 `.partial.pdf`와 실패 보고서를 남긴다.
`completed_with_warnings`는 검토할 경고가 있다는 뜻이다. 경고가 없어도 OCR 정답률을 보증하지 않는다.
`insertion failed`는 텍스트 삽입 실패, `boxes without assigned text`는 글자가 배정되지 않은
상자가 있다는 뜻이다. 후자는 잡음인지 실제 누락인지 원본을 확인한다.

## 모드와 주요 옵션

| 옵션 | 동작 |
|---|---|
| 기본 실행 | 자동 줄 정렬과 최종 페이지·문자·투명 텍스트·픽셀 검증 |
| `--fast` | 문단 중심 배치. 자동 모드의 전체 줄 OCR과 최종 검증을 생략 |
| `--paragraph` | 줄 검출 없이 문단 단위로 삽입하는 호환 모드 |
| `--debug-lines` | 배치 영역을 표시한 별도 `_auto_debug.pdf` 생성 |
| `--batch 10` | 문단 요청당 페이지 수. 기본 10, 0은 전체 |
| `--no-cache` | 문단 OCR을 다시 요청. 일치하는 재개·줄 캐시까지 모두 지우는 옵션은 아님 |
| `--repair-cache` | 저장된 문단의 누락 의심 영역 재검토 |
| `--no-repair` | 문단 누락 보완 생략. 줄 인식은 별도 |
| `--no-boundary-repair` | 블록 경계 잘림 보완 생략 |
| `--bookmarks` | 인쇄 목차와 본문을 대조한 책갈피 생성 |
| `--layout-review`, `--line-ocr-review` | 원본과 대조한 수동 검토 적용 |

디버그 출력은 테두리가 있으므로 픽셀 동일성 검사를 생략한다. 일반 실행과 완료 상태·보고서·
폴더 요약을 분리해 서로 덮어쓰거나 완료로 오인하지 않는다.
CPU 작업자, 메모리, 요청 겹치기, 서버 설정은 [성능 설정](PERFORMANCE.md)에 모았다.
책갈피 생성·갱신·교정은 [책갈피 안내](BOOKMARKS.md)를 참고한다.

## 캐시와 보완 인식

자동 모드는 원본이나 API 설정이 다른 캐시를 보존하고 별도 캐시를 사용한다.
빠른 모드와 선택 페이지 모드는 불일치 캐시를 거부한다. 원본 식별자가 없는 구형 캐시에
`--trust-cache`를 사용하려면 같은 원본인지 직접 확인해야 한다.
모델 변경 시 기존 DB를 보존하고 별도 출력 위치에서 실행한다.

경계 보완은 블록 가장자리의 잉크를 검사하고 공백까지 확장한 영역을 재인식한다.
기존 문자 순서·숫자를 보존하는 결과만 채택하며, 거부한 결과는 검토 경고로 남긴다.
원시 OCR 캐시는 변경하지 않고 원본·알고리즘에 연결된 파생 결과를 별도로 저장한다.
완전히 떨어진 누락 단어, 저대비 글자와 복잡한 그림까지 모두 복원하지는 않는다.
누락 보완의 원문·후보·채택 여부는 `repair_audit`에서 확인한다.

## 원본 대조 후 교정

일반 실행에는 교정 파일이 필요하지 않다. 교정 자료는 자신의 원본을 직접 확인하여 작성한다.
예제 경로의 `reviews/` 자료는 저장소에 포함하지 않는다.

`--layout-review 교정.json`은 원본 SHA-256과 교정 전 OCR 블록이 모두 일치할 때만 적용한다.
최상위 `source_sha256`, `pages`와 페이지별 `page`, `reason`, `original_blocks`, `lines`를 둔다.
각 줄은 `text`와 OCR 이미지 픽셀 좌표 `bbox: [x0, y0, x1, y1]`를 가진다.
검토한 줄·셀은 다시 분할하지 않고 직접 배치한다. 원시 OCR을 보존하며 파일 변경은
완료 상태 재사용 판단에 반영된다. 작은 합성 예제는 `tests/test_ocr_regressions.py`에 있다.

`--line-ocr-review 교정.json`은 줄 판단 기록의 `block_key`에
`original`, `replacement`, `reason`을 연결한다. 원문과 줄 이미지 식별자가 맞아야 적용한다.
원시 결과와 교정문을 함께 기록한다. 모델의 경계 판정이 거절돼도 식별자가 일치하는 명시적
교정문은 원래 줄 배치를 안내로 사용하여 적용한다. 감사 기록의 `accepted`는 경계 판정이며
적용 문구는 `reviewed`, `selected`에서 확인한다.

## 선택 페이지 줄 인식

```powershell
python run.py "input/book.pdf" --line-ocr-pages 13,17,26
```

페이지 번호는 PDF 첫 페이지가 1이며 `all`도 지원한다. 이 호환 모드는 HTML이 아닌
여러 줄 한글 문단을 대상으로 하므로 기본 자동 모드와 범위가 다르다.
출력은 `_line_searchable.pdf`, 디버그는 `_line_debug.pdf`다.
줄 응답은 `_line_ocr.json` 항목으로 저장하며 이미지와 API 설정이 같은 결과를 재사용한다.
경계 안내만 사용하고 문단 원문을 유지한다. API 오류가 나면 남은 미저장 이미지 요청을 멈추고
기존 배치로 처리하므로 기본 자동 모드의 오류 처리와 다르다.

## 문자·수식과 정확도의 한계

지원하는 간단한 TeX 수식은 검색 가능한 Unicode로 변환한다.
예를 들어 `\Delta` → Δ, `x_{0}` → x₀이며 지원하지 않는 명령·분수·행렬이 남으면
그 수식 전체를 그대로 유지한다. 원시 캐시와 Markdown은 보존한다.
PDF 문자 검사는 변환한 검색용 문자열을 기준으로 한다.

한글 줄 모델은 한글·라틴 문자·숫자를, 기본 줄 모델은 영문·한자·가나 등을 다룬다.
줄 모델 사전 밖의 글자는 정렬이 거부되어 대체 배치로 돌아갈 수 있다.
문단 OCR 원문을 줄 모델이 추측한 문자열로 바꾸지는 않는다.
복잡한 표·수식·희귀 문자와 부분적으로 기존 텍스트가 있는 페이지는 원본 대조가 필요하다.

```powershell
python run.py audit "output/book"
python run.py audit "output/book/book_auto_searchable.pdf"
```

감사는 잉크 커버리지, 문항 번호, 두 모델의 일치율로 검토할 곳을 찾는다.
번호 범위와 반복 횟수가 알려진 문제집에만 `--items 1-100 --copies 10` 등을 지정한다.
일반 책에는 이 옵션을 적용하지 않는다. 두 모델이 같은 곳에서 함께 틀릴 수 있으므로
일치율은 정답률이 아니다. 실제 문자 정확도는 원본과 대조한 기준 전사로 평가한다.
