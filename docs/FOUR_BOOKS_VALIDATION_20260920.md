# 4권 OCR·책갈피 최종 검사 — 2026-09-20

> 특정 입력과 당시 환경의 실험 기록이다. 원본, 교정 JSON, `tmp/` 및 문서별 fixture는
> 공개 저장소에 포함하지 않는 로컬 자료다.

원본 4개, 총 1,982쪽을 처리했다. 검색 가능한 PDF와 계층형 책갈피를 생성했고, 발견한 문제를 수정한 뒤 해당 결과를 다시 검증했다. 입력 PDF와 원시 OCR 캐시는 보존했다.

## 최종 파일

| 책 | PDF 쪽수 | 책갈피 | 용량(십진 GB, 약) | 결과 경로 |
|---|---:|---:|---:|---|
| 매트랩 프로그래밍 | 596 | 224 | 1.259 | `output/매트랩 프로그래밍/매트랩 프로그래밍_auto_searchable.pdf` |
| 쉽게 배우는 C 자료구조 | 422 | 212 | 0.825 | `output/쉽게 배우는 C 자료구조/쉽게 배우는 C 자료구조_auto_searchable.pdf` |
| 신호 및 시스템 | 400 | 161 | 0.882 | `output/신호 및 시스템/신호 및 시스템_auto_searchable.pdf` |
| 컴퓨터 구조론 | 564 | 140 | 1.067 | `output/컴퓨터 구조론/컴퓨터 구조론_auto_searchable.pdf` |

Goodnotes에 가져갈 파일은 위 PDF 4개다. `.ocr` 폴더는 재실행용 캐시·보고서·백업이며 가져올 필요가 없다. 원본 화질을 보존했으므로 파일 크기도 원본과 비슷하다.

## 검사 범위와 결과

- 모든 페이지에서 원본과 출력의 페이지 수, MediaBox/CropBox, 회전, 원본 이미지 스트림 해시가 일치했다.
- OCR 게시 전 전체 페이지의 72 dpi 픽셀 비교를 통과했다. 독립 검사에서는 권당 5쪽을 144 dpi로 추가 비교했다.
- PyMuPDF와 pypdf의 strict 모드로 모든 페이지를 읽었다. 페이지별 공백 제외 Unicode 문자 구성 차이는 0건이다. 이 검사는 인식 정답률이나 모든 수식의 의미·읽기 순서에 대한 보증이 아니다.
- NUL/대체문자 U+FFFD, 페이지 바깥으로 벗어난 텍스트, 미내장 폰트, 암호화, 복구가 필요한 PDF 구조를 검사했다. 검출 오류는 0건이다.
- 저장된 책갈피의 제목·계층·목적지 좌표가 계획과 일치했다. 마지막 책갈피 전용 수정에서는 모든 페이지의 픽셀·텍스트·좌표와 책갈피 외 객체/스트림이 이전 PDF와 동일함을 확인했다.
- 별도 복사본에서 Ink와 Highlight 주석을 추가하고 저장한 뒤 다시 열어 보존됨을 확인했다. 사용자 PDF에는 테스트 주석을 남기지 않았다.
- 텍스트 없는 페이지 23쪽을 원본 이미지로 확인했다. 빈 페이지 또는 글자 없는 표지였다. 커버리지 경고가 큰 페이지는 권당 2쪽을 추가로 시각 확인했다.
- `python run.py test`: **319개 통과**, 167.071초. 오류 복구를 의도적으로 발생시키는 테스트의 진단 메시지는 테스트 실패가 아니다.
- `python tools/check_architecture.py`: **101개 Python 파일, 28개 구현 모듈 통과**. 정의되지 않은 전역 이름·구문 오류·의존 순환 없음.

## 발견한 문제와 조치

1. Docker 초기 기동 중 `docker info` 시간 초과가 전체 작업을 중단하던 문제를 수정했다. 기존 대기 제한 안에서 재시도하며 회귀 테스트를 추가했다. 초기 실패한 2권은 서비스 복구 후 다시 실행하여 성공했다.
2. 전체 인쇄 목차와 장별 contents 패널을 구별하고, 쪽수 열이 누락된 중간 목차, 반복 제목, 부록 A/B 계층, 장 표지와 본문 절 제목을 구별하도록 책갈피 해석을 개선했다. 모델·렌더 배율·탐지 임계값은 바꾸지 않았다.
3. 원본 목차 이미지와 본문 이미지를 대조한 별도 검토 파일로 OCR 오독 제목·쪽수·목적지를 보정했다. C 자료구조 4장은 PDF 115쪽, 컴퓨터 구조론 7장은 PDF 395쪽 장 표지에 연결했다.
4. 매트랩 PDF 258·281·302·407·442쪽의 코드, 429·462쪽의 기호 표를 원본 이미지로 확인하여 별도 레이아웃 검토로 교정했다. C 자료구조 PDF 144쪽 코드도 교정했다. 신호 및 시스템 PDF 164쪽에서 검색층에 누락된 `4.3.3 연속 시간 푸리에 변환 성질` 제목을 복원했다. 원본 이미지 픽셀은 그대로다.
5. 수정 전 PDF·SQLite는 각 출력 폴더의 `.ocr/before_*`, `.ocr/bookmark_history`에 보존했다. 기존 작업 디렉터리의 변경 사항과 과거 결과도 유지했다.

## 남아 있는 원본 문제와 사용 범위

**C 자료구조 원본에는 인쇄 53–54쪽이 없다.** 원본 PDF 50쪽의 인쇄 쪽수는 52, 다음 PDF 51쪽은 55다. 따라서 인쇄 53쪽의 `Lab 2.1 행렬 표현하기` 책갈피 하나를 `source_page_missing`으로 보류했다. 없는 내용을 만들거나 다른 페이지로 연결하지 않았다. 나머지 3권의 미연결 책갈피는 0개다.

4권의 파이프라인 상태는 `completed_with_warnings`다. 남은 경고는 수식·표·장식 영역, 줄 정렬, 인식 모델 간 불일치 같은 품질 신호다. `audit`의 모델 간 일치율은 정답률이 아니며 전권을 사람이 글자 단위로 교정한 결과도 아니다. 검색·복사 시 수식의 LaTeX 표기와 일부 OCR 오타가 남을 수 있다. 보이는 페이지는 원본과 동일하게 보존했다.

실제 iPad/Goodnotes 앱에서 가져오기·동기화·필기 성능을 시험한 것은 아니다. PDF 구조와 표준 주석 저장은 검증했으며, 실제 기기의 대용량 파일 처리 성능은 별도 확인이 필요하다.

## 재현과 근거

최종 검토를 포함한 OCR 재실행 명령은 다음과 같다. 재실행 시 원본과 동일한 검토 파일을 함께 지정해야 한다.

```powershell
python run.py "input/매트랩 프로그래밍.pdf" --out "output/매트랩 프로그래밍" --bookmarks --bookmark-review reviews/bookmarks/matlab-final-bookmarks-20260920.json --layout-review reviews/matlab-final-layout-20260920.json
python run.py "input/쉽게 배우는 C 자료구조.pdf" --out "output/쉽게 배우는 C 자료구조" --bookmarks --bookmark-review reviews/bookmarks/cdata-final-bookmarks-20260920.json --layout-review reviews/cdata-final-layout-20260920.json
python run.py "input/신호 및 시스템.pdf" --out "output/신호 및 시스템" --bookmarks --bookmark-review reviews/bookmarks/signals-final-bookmarks-20260920.json --layout-review reviews/signals-final-layout-20260920.json
python run.py "input/컴퓨터 구조론.pdf" --out "output/컴퓨터 구조론" --bookmarks --bookmark-review reviews/bookmarks/computer-20260920.json
```

최종 PDF/원본 SHA-256과 요약 결과: `output/검사결과_20260920.json`.
독립 검사 스크립트: `tmp/goal_20260920_validate.py`.
권별 세부 결과·최종 audit·실행 로그·원본 대조 PNG: `tmp/goal_20260920/`.
최종 회귀 테스트 로그: `tmp/goal_20260920/final-tests.log`.
원본/레이아웃 해시와 직접 확인 근거는 `reviews/*20260920.json`, `reviews/bookmarks/*20260920.json`에 기록했다.
