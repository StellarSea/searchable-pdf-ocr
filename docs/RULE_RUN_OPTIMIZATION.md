# 결과를 유지하는 긴 획 탐지 최적화

## 변경 범위

`ocr_render._long_runs`는 가로로 지정 길이 이상 연속된 True 구간만 남긴다.
동일 함수를 전치한 마스크에도 적용하므로 세로선·표·문단 경계 탐지에도 쓰인다.
기존에는 전체 이미지 크기의 int32 누적 배열을 여러 번 만들었다. 이제 Boolean 입력은
약 1 Mi 셀 단위의 행 묶음에서 시작/끝을 찾고, 길이를 만족하는 구간만 복원한다.
매우 넓은 이미지는 한 행이 최소 처리 단위다. 최종 Boolean 출력 배열은 여전히 필요하다.

행 양끝의 False 패딩으로 행 사이의 획이 연결되지 않는다. 구간이 적으면 직접 채우고,
많으면 서로 겹치지 않는 구간의 +1/-1 표시를 int8 누적합으로 복원한다. 누적값은 0 또는 1뿐이다.
입력을 수정하지 않으며 전치·역순·읽기 전용 배열을 지원한다. 비 Boolean 입력의 과거
수치 연산과 잘못된 실수 길이의 예외는 별도 원래 경로로 유지한다.

모델, 해상도, 픽셀 임계값, 판정 기준은 바꾸지 않는다. 구현 파일 해시에 묶인 **파생 탐지 캐시**는
정상적으로 다시 계산된다. 원시 OCR 응답의 키와 PNG crop 키, 기존 결과는 그대로다.

## 별도 알고리즘 비교

WSL/Docker 재시작 후 동일 32페이지·동일 기록 OCR 응답을 사용해 reference → candidate →
candidate → reference 순서로 실행했다. 양쪽 모두 같은 display-list 재사용을 활성화했고,
HTTP 겹치기와 새 인식은 사용하지 않았다.

| CPU 보완 준비 | 이전 | 새 구현 |
|---|---:|---:|
| 첫 실행 | 59.201초 | 35.931초 |
| 둘째 실행 | 59.502초 | 35.805초 |
| 중앙값 | 59.351초 | 35.868초 |

보완 준비 단계 약 39.6% 단축. 매 실행의 405개 탐지 좌표, 9개 PNG SHA-256,
보완 텍스트·판정·페이지 체크포인트가 모두 같았다. 이는 전체 OCR 속도나 새 모델 인식의
정답률을 측정한 것이 아니며 GPU 사용률 100% 유지의 증거도 아니다.

합성 432만 셀의 함수 단독 비교에서는 일반 텍스트/선 마스크의 할당 최고치가
약 90.3 MB → 9.8 MB로 줄었다. 이는 tracemalloc 할당량이며 프로세스 RSS가 아니다.
조밀한 TTFF 반복도 결과가 같았고 약 90.8 MB → 23.4 MB였다.

기본 회귀는 폭 0~14의 모든 Boolean 패턴, 임계 길이, 별도 구간 정의,
입력 stride/전치, chunk 경계, 조밀한 구간, 빈 축과 과거 비 Boolean 입력을 검사한다.

적용 후 32페이지 overlay에서는 탐지/crop 인덱스를 가져오지 않고 PNG 응답 771개만
복사해 모든 좌표와 PNG를 새로 계산했다. HTTP 호출 0회, 기존 최종 PDF와 전 페이지
글자·좌표·픽셀·품질 판정이 같았다. CPU 풀 실제 9개(상한 16개), 32페이지 준비,
순차 복구 0회, 추가 작업자 RSS 최고 약 1.89 GiB였다. overlay 19.631초 + 순차 검증
17.785초는 이 캐시 고정 재생의 시간이며 새 OCR 전체 처리 시간과 비교하면 안 된다.
기존의 미배정 박스 경고 48개도 유지됐으므로 문서가 완벽해졌다는 뜻은 아니다.

적용 후 전체 오프라인 회귀 203개가 154.361초에 통과했고, Python 파일 76개 문법/
미정의 전역 검사 및 구현 모듈 23개 순환 의존성 검사가 통과했다.

## 재현과 복구

```powershell
python -m unittest discover -s tests -p test_rule_runs.py -v
python tools/benchmark_rule_runs.py --out tmp/new-rule-runs.json
python tools/benchmark_repair_render.py --source tmp/cpu_workers/production32/source.pdf --baseline tmp/performance-max/server-reader-full-output --out tmp/new-rule-repair --rule-runs
python tools/benchmark_cpu_startup.py --source tmp/cpu_workers/production32/source.pdf --baseline tmp/performance-max/server-reader-full-output --out tmp/new-rule-overlay --offline-replay
```

원시 비교는 `tmp/performance-max/rule-runs-micro.json`, `rule-runs-repair/`에 있다.
수정 직전 렌더 모듈은 `tmp/performance-max/rule-runs-rollback/ocr_render.py`에 보관했다.
복구가 필요하면 OCR이 멈춘 상태에서 해당 함수와 수치 보조 함수만 비교해 되돌린다.
후속 수정까지 덮어쓰는 전체 파일 복사는 피한다. 서버 재시작은 이 변경의 적용에 필요 없다.
