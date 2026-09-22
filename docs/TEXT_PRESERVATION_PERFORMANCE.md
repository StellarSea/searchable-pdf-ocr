# 문자 보존과 비교 비용 개선

2026-09-14 적용. CPU/GPU 병렬 준비 설정을 유지하면서 텍스트 후처리의 유실과
불필요한 CPU 계산을 줄였다. 모델·입력 해상도·탐지 임계값·원본/이미지 캐시 키는
변경하지 않았다. 기존 운영 PDF와 DB, 실행 중인 서비스는 수정하지 않았다.

## 정확도 관련 수정

- HTML 문단에 섞인 `<Node>값</Node>`의 닫는 태그를 대소문자와 함께 보존한다.
  `<Item/>`은 한 번만 남기며 `<p-value>`를 `<p>` 서식으로 오인하지 않는다.
- 표 셀의 `vector<T>`에서 `<T>`가 사라지던 문제를 수정했다. 셀에서 해제된
  HTML entity는 후속 평문 변환 전에 다시 escape하여, `&lt;b&gt;x&lt;/b&gt;`가
  실제 내용 `<b>x</b>` 대신 `x`로 축약되는 이중 해석을 막는다.
- 숫자/주소를 개수만 비교하던 안전 판정과 검토 경고에서 **등장 순서도 대조**한다.
  긴 문장의 `first=12;second=34`가 `first=34;second=12`로 바뀌면 거부/경고한다.
  근사 줄 인식은 계속 경계 안내로만 쓰며 문단 원문을 대신 삽입하지 않는다.

기록된 실제 응답 133개 중 한 항목(29페이지)은 숫자열이 `0, 0, 6.12`에서
`0, 6.12, 0`으로 바뀌었다. 과거 `line_refinement_is_safe`는 이를 통과시켰으나
새 구현은 거부한다. 그 항목의 기존 `recognition_disagrees` 경고는 이미 켜져
있었으므로 실제 페이지에 새 경고를 발견했다는 뜻은 아니다.

HTMLParser가 닫는 태그 이름을 소문자로만 전달하기 때문에, 파서의 `parse_endtag`
진입점에서 원문 토큰을 보관한다. 조각별 feed, 공백 있는 닫는 태그, 알려진 HTML의
빈 태그도 회귀로 검사한다. 지원 HTML 태그 자체를 코드로 쓰는 경우에는 기존처럼
`&lt;...&gt;`로 표현해야 서식과 구별할 수 있다.

## 성능 변경의 범위

- 정규화 후 동일한 문자열은 SequenceMatcher를 실행하지 않는다. 줄 위치 정렬과
  감사 점수에도 같은 최단 경로를 적용한다.
- 다른 문자열은 기존 정확한 유사도 계산 전에 길이/문자 개수 기반 상한으로 탈락
  여부를 확인한다. 상한이 충분하면 기존 `autojunk=False` 계산을 그대로 수행한다.
  인식 임계값 0.88/0.94/0.97은 유지한다.
- 주소 기호 `@`가 없는 문단에는 숫자 정규식만 적용한다. 긴 공백 제거 문장의
  모든 위치에서 이메일 패턴을 시도하던 반복 스캔을 피한다.

이 변경은 OCR 모델 정답률이나 전체 책 처리 속도를 보장하지 않는다. 개선되는
영역은 후처리 문자 보존, 오류 후보 감지, 텍스트 비교 단계다. 이미 완료된 결과는
자동 재작성하지 않으며 원시 OCR 캐시도 변경하지 않는다.

## 재현

```powershell
python -m unittest discover -s tests -p test_text_preservation.py -v
python tools/benchmark_text_processing.py --baseline-module tmp/text-improvements-baseline/ocr_text.py --database tmp/performance-max/vram-controlled-a2-full-output/.ocr/source_line_ocr.sqlite3 --out tmp/NEW-text-benchmark.json
python tools/benchmark_cpu_startup.py --source tmp/cpu_workers/production32/source.pdf --baseline tmp/performance-max/vram-controlled-a2-full-output --out tmp/NEW-text-pdf --offline-replay
python run.py test
python tools/check_architecture.py
```

벤치마크는 명시적으로 지정한 수정 전 모듈을 읽고 DB를 읽기 전용으로 연다.
실제 기록 응답과 합성 반복 문자열을 구분하며 각 세 번을 교차 실행한다.
변화한 행은 숨기지 않고 `changed_rows`에 기록한다. 출력 파일이 있으면 중단한다.
속도 비교는 다른 회귀/벤치마크가 끝난 뒤 실행한다.

## 검증 결과

전체 오프라인 회귀 247개가 161.987초에 통과했다. 이후 표 셀의 실제 좌표 검사를
추가한 최종 문자 보존 테스트 9개도 모두 통과했다(기존 8개 포함, 고유 검사 총 248개).
숫자 순서, Unicode/HTML entity, 동일 문자열의 줄 경계, PDF 문자와 위치, 화면 픽셀을
검사했다. 임의 문자열 103쌍/임계값 7개는 기존 정확한 SequenceMatcher 판정과 같다.

다른 회귀 종료 후 세 번 교차 실행한 텍스트 비교 결과:

| 입력 | 수정 전 중앙값 | 수정 후 중앙값 | 출력 비교 |
|---|---:|---:|---|
| 실제 기록 133개 블록 | 43.04ms | 29.23ms | 위 29페이지 안전 판정 1개만 강화 |
| 반복 문구 합성 5,700자 | 1,171.69ms | 1.11ms | 판정과 줄 분할 동일 |

실제 기록의 비교 단계는 약 32% 단축됐지만 절대 절감은 약 14ms다. 합성 표본은
긴 반복 문자열에서 발생하는 병적인 비용을 확인하기 위한 것이며 일반 문서의
평균 성능이나 전체 OCR 시간에 해당 배율을 적용하지 않는다.
원시 결과는 `tmp/text-improvements/benchmark-final.json`이다.

실제 32페이지도 기준 원시 PNG 응답 771개만 새 임시 캐시에 복사하여 재생했다.
탐지/PNG를 다시 생성했고 새 HTTP 요청은 0회였다. 전 페이지 글자·글자 좌표·화면
픽셀·페이지 판정·줄 판정이 기준과 정확히 같았다. CPU 준비는 상한 16개 중 메모리
여유에 맞춘 실제 13개가 32페이지를 처리했으며 순차 복구는 없었다. 이 실행의
overlay 16.673초/검증 13.870초는 단일 확인 실행이며 이전 전체 OCR과 속도 비교하지 않는다.
기존 미배정 줄 상자 48개는 남아 있다. 기준/운영 결과를 덮어쓰지 않았다.
원시 결과는 `tmp/text-improvements/pdf-replay/comparison.json`이다.

최종 아키텍처 검사는 Python 파일 90개/구현 모듈 24개를 통과했다.
