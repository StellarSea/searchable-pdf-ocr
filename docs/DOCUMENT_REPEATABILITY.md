# 문단 OCR 반복성과 GPU 공급 공백 진단

2026-09-14, 실행 중인 사용자 OCR이 없는 것을 확인한 뒤 기존 서버에 진단 요청만 보냈다.
원본·운영 PDF/SQLite·모델·Docker 설정은 변경하거나 재시작하지 않았다. HPS도 사용하지 않았다.

## 실제 서버 설정 확인

- 실행 중인 vLLM은 **0.10.2**다. 파이프라인은 PaddleOCR-VL-1.6,
  VLRecognition backend는 `vllm-server`다.
- 설치된 PaddleX `inference/models/doc_vlm/predictor.py`의 502–506행에서
  이 backend는 temperature가 지정되지 않으면 **0**을 요청에 넣는다.
  HTTP schema는 미지정 값을 None으로 전달한다. 현재 클라이언트도 이를 덮어쓰지 않는다.
- 따라서 "temperature를 0으로 바꾸면 해결"이라는 제안은 현재 설정에 대한 개선이 아니다.
- 설치된 vLLM `envs.py`에는 `VLLM_BATCH_INVARIANT`가 없다. 최신 문서의 옵션을 현재
  이미지에 추가하는 것만으로 기능이 생긴다고 가정하지 않는다.

vLLM [0.10.2 재현성 문서](https://docs.vllm.ai/en/v0.10.2/usage/reproducibility.html)는
해당 버전 온라인 서빙의 재현성을 지원하지 않는다고 설명한다. 최신 버전의
[batch invariance](https://docs.vllm.ai/en/stable/features/batch_invariance/)는 별도 기능이다.
이는 현재 오류의 구체적인 CUDA 연산/스케줄 원인이 입증됐다는 뜻이 아니다.
버전 업그레이드, 엔진 다중 프로세싱 해제, 정밀도 변경은 이번에 적용하지 않았다.

## 같은 10페이지 바이트를 3회 순차 전송

`tools/benchmark_document_repeat.py`는 원래 `split_pdf`가 만든 첫 10페이지를 한 번만
메모리에 만들고, 같은 bytes와 API 옵션을 같은 서버에 순차 전송한다. 자동 재시도·캐시·
경계 보완·line-ocr·overlay·검증은 실행하지 않는다. 새 진단 폴더에 원래 요청과 각 응답을
기록하고, 블록 문자열/박스/label/id/order의 차이가 있으면 종료 코드 1을 낸다.
비교의 기준 응답을 정답으로 간주하지 않는다.

- 입력: `tmp/cpu_workers/production32/source.pdf`의 첫 10페이지, 11,839,113바이트.
- 입력 SHA-256: `310041e180dbbedefcb5b6a30ad4341194ca3ec851c00602e607a8ba81963828`.
- 3회 시간: **19.155 / 15.800 / 12.853초**, 각 153블록. 순서 효과/워밍업이 있어
  이를 서로 다른 설정의 속도 개선으로 해석하지 않는다.
- 첫 응답 대비 두 번째는 표본 p5/b18, p6/b11, p10/b3의 문자열이 다르다.
  이 세 차이는 공백/줄바꿈뿐이다.
- 세 번째는 p5/b18, p10/b3, p10/b19가 다르다. 마지막 블록은 공백뿐 아니라
  `바끄는`과 `바꾸는`의 차이가 있다. 해당 원본의 정답 판정은 이 실험에서 하지 않았다.
- 비교한 블록의 박스·label·id·order는 동일하다. 전체 응답 JSON의 모든 부가 필드를
  동일하다고 주장하지 않는다.
- 앞서 확인된 p3의 상수 `17→1` 오류는 이번 세 응답에서 재현되지 않았고 모두 `17`이었다.

원시 결과: `tmp/performance-max/repeat-document/`. 자원 기록: `repeat-profile/`.
프로파일러 종료 코드 1은 요청 실패가 아니라 **반복 결과의 차이를 발견했다는 판정**이다.

## GPU 저사용 구간의 새로운 관측

약 1초 간격 자원 표본에서 GPU와 VLM metrics 각각의 나이가 2초 이하인 48개를 사용했다.
GPU 평균 36.29%, GPU 사용률 ≤10%는 17개였다. **이 17개 모두에서 VLM 실행/대기 요청이 0**이었다.
특히 요청 전송 직후 수초간 VLM에 요청이 아직 보이지 않다가 GPU 사용률이 올라갔다.

이 실험은 클라이언트 PDF 분할을 한 번만 하고 동일 bytes를 바로 보낸다. 따라서 문단 OCR의
공백을 클라이언트 line-ocr 준비/overlay 병목으로 설명할 수 없다. 다음 측정 대상은
서버의 PDF 읽기/이미지 변환·layout 탐지·crop 준비·VLM 전송 전 인코딩이다.
그중 어느 함수가 몇 초를 차지하는지는 현재 지표만으로 구분하지 못한다.

GPU와 metrics는 비동기 표본이며, 짧은 요청을 놓칠 수 있다. 17개를 정확히 17초의
완전 유휴 시간으로 바꾸거나, 모든 책/line-ocr 단계에 같은 원인을 적용하지 않는다.
또한 다른 GPU 작업 및 측정 경계 구간이 포함될 수 있다.

## 문단 crop 단독 확인

이미 보관한 p3/b4 검토용 PNG를 기존 경계 보완과 같은 `useLayoutDetection=False` 옵션으로
4회 순차 인식했다. 시간은 1.473 / 1.311 / 1.184 / 1.551초이며 네 응답은 같고 상수는 `17`이다.
이 crop은 검토용 크기로 렌더링된 것으로 서버가 PDF에서 만든 내부 crop과 동일한 픽셀이 아니다.
따라서 단독 결과를 근거로 문단별 요청 전환이 정확도를 보장한다거나 배치가 오류의 유일한
원인이라고 결론 내리지 않는다.

원시 결과: `repeat-crop-default/`. 첫 진단 `repeat-crop/`는 추가 promptLabel 옵션을 넣었을 때
HTTP 422로 인식 전에 실패했다. 이를 제외하고 기존 옵션 그대로 다시 측정했다.

## 재현과 다음 단계

```powershell
python tools/profile_ocr_resources.py --out tmp/NEW-profile -- python -u tools/benchmark_document_repeat.py --source tmp/cpu_workers/production32/source.pdf --out tmp/NEW-repeat --batch 10 --repeats 3 --live
```

GPU 요청 수를 무작정 늘리는 것보다 서버의 인식 전 준비 시간을 먼저 분리 측정한다.
설치된 파이프라인의 `layout_prep_cpu_workers`는 설정 파일에 없어 현재 기본 0이며,
이 옵션만으로 전체 준비 시간이 줄어드는지 아직 검증하지 않았다. 이 코드가 공식 원본인지
로컬 패치인지도 확인한 뒤 비교해야 한다. 모든 변경은 고정 입력의 좌표/이미지 동일성과
새 인식의 문자 변동을 분리하여 평가한다. 기존 캐시와 모델은 그대로 보존한다.

후속 확인: 해당 CPU 준비 옵션은 PaddleX v3.6.1 공식 소스에도 있다.
분리 측정에서 더 큰 비용은 PDF 이미지 변환이었으며, 세부 결과와 공유 메모리 후보는
`SERVER_PREPARATION.md`에 기록했다. 옵션을 운영에서 켜지는 않았다.

진단 도구의 오프라인 회귀 4개와 67개 Python 파일 구조 검사가 통과했다.
이번에는 운영 구현을 바꾸지 않았으므로 전체 OCR 성능/정확도 개선이 완료된 상태는 아니다.
