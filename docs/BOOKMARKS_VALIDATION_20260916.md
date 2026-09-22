# 책갈피 개선 검증 — 2026-09-16

> 특정 입력과 당시 환경의 실험 기록이다. 원본, 교정 JSON, `tmp/` 및 문서별 fixture는
> 공개 저장소에 포함하지 않는 로컬 자료다.

## 두 책의 결과

| 책 | 이전 생성 수 | 원본 목차 항목 | 개선 후 | 보류 |
|---|---:|---:|---:|---|
| 신호 및 시스템 | 124 | 161 | 161 | 없음 |
| 쉽게 배우는 C 자료구조 | 46 | 213 | 212 | 원본에 없는 인쇄 53쪽 1개 |

신호 책의 목차 PDF 5~10쪽, C 자료구조의 목차 PDF 6~12쪽을 모두 이미지로 확인했다.
쪽수 없는 장 제목, 제목과 쪽수가 여러 줄에 나뉜 항목, 장식 문자, 장 소개의 절 목록을
구분하도록 수정했다. 원본 이미지에서 확인한 OCR 오독은 `reviews/bookmarks/`의
원본·레이아웃 SHA-256에 묶인 별도 파일로 책갈피에만 교정했다.

### 목적지 대조

- 신호: 인쇄 쪽수가 있는 모든 항목은 PDF 쪽수 = 인쇄 쪽수 − 2와 일치.
  장 시작은 PDF 11, 37, 81, 135, 185, 219, 239, 273, 315, 343쪽.
  잘못 연결됐던 3.1은 PDF 82쪽, 8.1은 PDF 274쪽으로 수정.
- C 자료구조: 인쇄 52쪽까지 PDF 쪽수 = 인쇄 쪽수 − 2, 인쇄 55쪽부터 − 4와 일치.
  장 시작은 PDF 13, 43, 71, 115, 151, 193, 239, 297, 341, 379쪽.
  원본 PDF 50쪽의 인쇄 번호는 52, 바로 다음 PDF 51쪽은 55다.
  따라서 목차의 `Lab 2.1 행렬 표현하기`(인쇄 53쪽)는 `source_page_missing`으로 남겼다.
- 모든 저장 항목의 제목·계층·PDF 페이지·이동 좌표를 다시 열어 확인했다.
  위 쪽수 관계는 이 두 원본의 독립 검증 기준이며 모든 문서에 적용하는 고정값이 아니다.

## OCR 보존

기존 완성 PDF 대비 신호 400쪽, C 자료구조 422쪽, 합계 **822쪽 전체**를 비교했다.

- 모든 페이지의 72dpi 렌더링 픽셀 SHA-256 일치.
- 모든 페이지의 추출 Unicode 문자, 문자 좌표, 페이지 크기·회전·잘림 영역 일치.
- 책갈피 객체와 카탈로그 Outlines 참조를 제외한 기존 PDF 객체 및 원시 스트림 일치.
- 원본 PDF SHA-256 유지. OCR API 호출 및 모델·인식 설정 변경 없음.
- 두 SQLite의 responses, crop_index, decisions, detections 및 보고서/상태를 제외한
  artifacts: 10개 테이블의 행 수와 정렬한 행 해시가 작업 전후 일치.

이는 기존 OCR의 보존 검증이며 기존 OCR 자체의 정답률을 보증하지 않는다.
목차 원본 확인에 따른 책갈피 표시 교정은 OCR 본문과 분리했다.

## 결과 파일 및 백업

- 신호: `input/ocr_output/신호 및 시스템_bookmarked.pdf`.
  기존 `신호 및 시스템_auto_searchable.pdf` 교체를 Windows가 거부하여 별도 개선본으로 저장했다.
  별도 파일 SHA-256은 400쪽 보존 검증을 통과한 후보와 동일하다.
- C 자료구조: `input/ocr_output/쉽게 배우는 C 자료구조_auto_searchable.pdf` 갱신 완료.
- 신호 백업/검증 보고서:
  `input/ocr_output/.ocr/bookmark_history/신호 및 시스템/20260916_003835_31f5a7ee/`.
- C 자료구조 백업/검증 보고서:
  `input/ocr_output/.ocr/bookmark_history/쉽게 배우는 C 자료구조/20260916_004137_c8525743/`.
- 상세 검사 결과와 캐시 비교: `tmp/bookmarks_v2/final_verification.json`,
  `cache_preservation_before.json`, `cache_preservation_after.json`.

기존 신호 PDF의 잠금이 해제되면 전체 렌더링을 반복하지 않고 다음 명령으로 게시를 재시도할 수 있다.
원본·기존 PDF·백업·검증 후보·보고서가 검사 당시 그대로인 경우에만 교체한다.

```powershell
python run.py bookmarks --publish-verified "input/ocr_output/.ocr/bookmark_history/신호 및 시스템/20260916_003835_31f5a7ee"
```

## 코드 검증

`python run.py test`: **292개 통과**(184.219초). 로그: `tmp/bookmarks_v2/final_tests.log`.
`python tools/check_architecture.py`: Python 99개 파일 구문/정의 검사와 구현 모듈 28개의 순환 의존 검사 통과.
새 회귀는 분리 쪽수, 장 소개 오연결, 한 글자 장 제목, 원본 페이지 누락, 제한적인 오독 비교,
교정 파일 식별, 사용자 책갈피 보호, OCR 보존 실패 시 게시 금지 및 검증 후보 변조 시 재게시 거부를 다룬다.
