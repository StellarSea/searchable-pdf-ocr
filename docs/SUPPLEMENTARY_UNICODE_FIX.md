# 보충 유니코드 문자 때문에 최종 PDF 검증이 실패한 사례

## 확인한 원인

`신호 및 시스템.pdf` 400페이지 실행은 `extracted_text_mismatch` 하나 때문에
최종 게시를 중단했다. 실패한 페이지는 PDF 73페이지였고, 기대 문자 `🏢`
(U+1F3E2) 1개가 추출 결과에서 `ὀ`(U+1F40)로 바뀌었다. 화면 변경이나
티베트 문자 U+0F5A의 폰트 탐색 로그가 직접 원인은 아니었다.

설치된 PyMuPDF 1.28.2가 새로 포함한 Segoe UI Symbol의 ToUnicode CMap에는
`<0d52> <0d62> <1f3e0>` 같은 5자리 목적값이 있었다. 이 PDF의 glyph 3412는
`🏢`를 그리지만 텍스트 추출은 잘못된 매핑을 따른다. 보충 문자는
[Unicode UTF-16 설명](https://unicode.org/faq/utf_bom.html)에 따른 surrogate pair가
필요하며, U+1F3E2의 올바른 UTF-16BE 목적값은 `<d83cdfe2>`다.

원본 73페이지 아래쪽을 직접 확인한 결과, 모델이 원본 **“풀이”의 “풀”을
`🏢`로 오인식**한 별도의 문제도 있었다. 이번 저장 오류 수정은 그 문자를
원본 정답으로 확정하거나 “풀”로 바꾸는 작업이 아니다. 원문 대조가 필요한
이 사례를 기록하고 원시 OCR 문자열은 보존한다.

## 수정 범위

- `ocr_fonts.repair_generated_font_unicode`는 원본을 연 시점의 xref 경계를 받아,
  이번 overlay가 새로 만든 폰트와 새 ToUnicode 스트림만 처리한다.
- `bfchar`/`bfrange`의 잘못된 5·6자리 목적값을 UTF-16BE로 바꾼다. 범위는
  명시적인 배열로 표현해 surrogate 경계를 넘더라도 정확히 매핑한다.
- 기존 BMP 매핑, 이미 올바른 surrogate pair, 여러 글자 목적값은 보존한다.
  원본 폰트나 원본과 공유한 ToUnicode 스트림은 수정하지 않는다.
- PDF 소유 스레드에서 저장 직전에 한 번 실행한다. 글리프 ID/폰트 프로그램,
  이미지/좌표, OCR 모델/캐시 키와 기존 검증 기준은 바꾸지 않는다.
- 검증 오류 메시지에는 실제 `SQLite 경로::보고서 항목`을 표시한다. 이전 최종
  파일이 없는 경우에는 “previous final PDF retained”라고 안내하지 않는다.

## 회귀 및 복구

전체 회귀 253개(147.801초)가 통과했다. 이후 오류 메시지 검사 1개를 추가한
최종 폰트 회귀 6개도 통과했다(고유 검사 총 254개). PyMuPDF와 pypdf에서
동일 보충 문자를 추출하고, 검색 위치 및 원본 화면 픽셀을 함께 검사한다.
원본 CMap 보존, 범위 경계, 정상 매핑의 무변경과 반복 적용도 검사한다.

```powershell
python -m unittest discover -s tests -p test_font_unicode.py -v
python run.py test
python tools/check_architecture.py
```

원래 실패한 부분 PDF와 SQLite는 `tmp/font-unicode-recovery/before/`에 복사했다.
오프라인 복구 스크립트는 OCR API/줄 인식/서비스 시작·재시작을 모두 금지한 상태에서
정상 CLI 경로를 재실행한다. 원시 문단/Markdown/원본 식별자/경계 결과와 줄 응답을
전후 비교하고, 400페이지 검증이 통과해야 최종 PDF와 성공 보고서를 게시한다.
기존 실패 보고서는 진단용으로 유지한다. 로그·요약은 `tmp/font-unicode-recovery/`다.

복구 실행은 120.75초에 완료됐고, 400페이지 전체 픽셀·문자·투명 텍스트 검증을
통과했다. 새 OCR 요청 0회, 원시 OCR 항목과 줄 응답의 전후 해시가 모두 같았다.
최종 파일은 `input/ocr_output/신호 및 시스템_auto_searchable.pdf`다.
실제 73페이지도 PyMuPDF와 pypdf 양쪽에서 U+1F3E2가 1개씩 추출됐으며, 해당
원본/결과 crop의 픽셀을 비교하고 렌더링을 눈으로 확인했다.

완료 상태는 `completed_with_warnings`다. 기존 검토 대상 364페이지와 미배정
줄 상자 545개를 정답으로 확정하거나 숨기지 않았다. 이 경고 수치는 364페이지가
틀렸다는 뜻이 아니며 수식/복잡한 구조 등의 일반 검토 신호도 포함한다. 위에서
원본으로 확인한 “풀이” 오인식은 후속 원본 대조 교정 대상이다.
