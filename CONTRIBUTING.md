# 기여 안내

먼저 [구조와 불변 조건](docs/ARCHITECTURE.md) 및 [AGENTS.md](AGENTS.md)를 읽습니다.
동작 변경, 알고리즘 실험, 구조 정리는 각각 검토할 수 있게 나눠 주세요.

## 개발 환경

현재 기준은 Windows / Python 3.13입니다. 저장소 루트에서:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints-tested.txt
.\.venv\Scripts\python.exe tools/check_architecture.py
.\.venv\Scripts\python.exe tools/check_publication.py --history
.\.venv\Scripts\python.exe run.py test
```

모델·GPU·Docker는 이 검사에 필요 없습니다. 시험 입력은 임시 폴더에서 만들고 HTTP와
모델 생성은 대체합니다. 실험을 위해 실행 중인 OCR 작업이나 서비스를 재시작하지 않습니다.
Windows 전용 폰트 시험은 해당 폰트가 없으면 건너뜁니다. 다른 플랫폼의 통과는 별도 검증합니다.

## 변경 원칙

- 원본 픽셀, Unicode, 읽기 순서, 원본과 연결된 캐시/검토 식별자를 보존합니다.
- 새 코어 코드는 소유 모듈에서 import합니다. `ocr_to_searchable_pdf.py`는 기존 import 호환을 유지합니다.
- PyMuPDF를 여러 스레드에서 호출하지 않습니다. PDF/SQLite 변경은 단일 소유자가 수행합니다.
- 구조 정리에 모델, 입력 폭, 렌더링 배율, 임계값, 캐시 namespace 변경을 섞지 않습니다.
- 동작 변경에는 회귀 테스트를 추가합니다. 레이아웃 변경은 글자·박스·픽셀을 비교합니다.
- 가능하면 테스트에 명시적 API 콜백을 주입합니다. 새 모듈 전역 monkey-patch 의존은 피합니다.
- 기존 CLI·파일명·스키마·캐시 키를 유지합니다. 필요한 migration은 문서와 실패/복구 테스트를 함께 작성합니다.

테스트를 통과했다는 이유로 실제 책 전체의 정확도를 주장하지 않습니다. 성능 수치는
하드웨어, 입력 범위, cold/warm cache, 옵션, 정확도 보존 검사와 함께 기록합니다.

## PR과 이슈

문제와 최종 동작을 먼저 설명하고, 관련 회귀 테스트 및 알려진 제한을 적습니다.
구조 변경에는 모듈 소유권, 저장 형식 변경에는 이관/복구 방법을 포함합니다.
`input/`, `output/`, `models/`, `reviews/`, 캐시, 개인 `.env`, 책 원문은 올리지 않습니다.
재현 자료는 직접 만든 작은 문서나 재배포 권한이 확인된 자료를 사용합니다.

`check_publication.py`는 현재 Git 추적/추적 후보 파일의 경로, 일부 알려진 인증정보 패턴,
상대 Markdown 링크를 검사합니다. `--history`는 모든 reachable commit도 검사합니다.
전체 비밀정보 탐지기나 저작권 판정기가 아니므로 게시 diff도 직접 검토합니다.

## 이력 관리

기존 이력을 임의로 재작성하거나 사용자 변경을 폐기하지 않습니다. 완성된 변경은 목적별
커밋으로 남기고, 실제로 존재하지 않았던 중간 구현 과정을 만들어 커밋하지 않습니다.
버전 태그는 검증한 커밋에만 붙이고 변경 내용은 [CHANGELOG](CHANGELOG.md)에 기록합니다.
라이선스 선택 전에는 외부 기여의 허가 조건도 확정되지 않았음을 명확히 합니다.
