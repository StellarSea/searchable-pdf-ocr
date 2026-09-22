# GitHub 공개 준비 기록

확인일: 2026-09-22. 이 기록은 로컬 공개 준비에 관한 것이며 원격 저장소 게시 기록이 아니다.

## 이력과 파일 범위

기존 `597e415`까지의 20개 커밋을 보존하고 `codex/public-release` 브랜치에 누적 구현,
공개 검사/설정, 문서 정리를 남긴다. 기존 revert를 지우거나 과거 구현 과정을 새로 만들지 않는다.
커밋 작성자 이름·이메일·시간은 Git 이력의 일부로 유지된다. 공개 시 이 메타데이터도 공개된다.

공개 대상은 Python 코드, 합성 입력 기반 테스트, 설정 예제와 문서다.
다음 로컬 자료는 삭제하지 않고 Git 후보에서 제외한다.

- 원본/결과/스캔/모델: `input/`, `output/`, `scans/`, `models/`, PDF/TIFF
- 문서 내용이 담긴 `reviews/`, `docs/fixtures/`, `tools/build_toc_review.py`
- SQLite와 journal/WAL, `.ocr/`, `.ocr_cache/`, 로그, `tmp/`, 가상환경
- 개인 `.env`와 개인 키 파일

기존 `compose/.env`는 이미지 태그 2개와 backend 선택만 포함했다. 현재 버전에서는
추적을 해제하고 `.env.example`을 제공한다. 과거의 정확히 그 세 설정만 검사에서 허용하며
다른 내용의 `.env`를 과거 이력에서 발견하면 실패한다. 원래 로컬 `.env`는 보존한다.

## 자동 검사

```powershell
python tools/check_publication.py --history
python tools/check_architecture.py
python run.py test
git diff --check
```

공개 검사는 추적/추적 후보 파일의 금지 경로, 5 MiB 초과 파일, 일부 알려진 인증정보
패턴과 상대 Markdown 파일 링크를 확인한다. 이력 검사는 reachable commit의 blob을 읽는다.
매치된 비밀값은 출력하지 않는다. 모든 형식의 인증정보·개인정보·저작권 문제를 탐지하는
도구는 아니며, Git reflog/unreachable object는 원격에 게시할 이력 범위에 포함하지 않는다.

이번 준비의 기존 오프라인 회귀 테스트는 Windows / Python 3.13.15에서 **319개 통과**했다.
공개 검사 회귀 4개를 추가한 후 `git clone --no-hardlinks --single-branch`로 공개 브랜치만
복제하고 복제본의 `run.py test`를 실행하여 **323개가 200.614초에 통과**했다.
복제본에는 원본·모델·교정 자료와 개인 `.env`가 없으며, 같은 호스트 Python 패키지를 사용했다.
이는 신규 머신에서 pip/Docker/모델 설치 전체를 재현한 시험은 아니다.

| 확인 | 결과 |
|---|---|
| 공개본 오프라인 회귀 | 323개 통과 |
| 공개본 구조 | Python 102개 문법/미정의 전역 검사, 구현 모듈 28개 순환 의존성 검사 통과 |
| 공개 범위·링크·이력 | 후보 파일 156개, reachable commit 23개 검사 통과 |
| 설치된 호스트 의존성 | `python -m pip check` 통과 |
| CLI와 diff | 도움말 실행 및 `git diff --check` 통과 |

실제 OCR 호출, GPU 재벤치마크, Docker 재시작은 이 작업에 포함하지 않는다.

GitHub Actions는 Windows/Python 3.13에서 같은 오프라인 검사를 실행하도록 구성했다.
워크플로 파일을 작성한 것과 GitHub에서 실행해 통과한 것은 구분한다.

## 배포 전 남은 선택

프로젝트 LICENSE는 아직 선택하지 않았다. 사용자가 라이선스별 특성을 검토 중이다.
[의존성 조사](DEPENDENCIES.md)에 PyMuPDF AGPL/상용 조건과 선택지를 정리했다.
원격 URL도 아직 설정되지 않았으며 GitHub 생성·push·릴리스 게시를 수행하지 않았다.

라이선스가 확정되면 LICENSE와 README 상태를 함께 갱신한다. 게시할 때는 현재 브랜치의
파일 목록과 diff를 확인하고, 전체 폴더 업로드 대신 검증한 Git 커밋을 사용한다.
이미지나 모델 가중치를 배포물에 넣으려면 별도의 실제 구성요소·라이선스 조사가 필요하다.
