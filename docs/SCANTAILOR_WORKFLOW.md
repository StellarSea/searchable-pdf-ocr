# ScanTailor Advanced에서 OCR PDF까지

대상: Windows 64비트 ScanTailor Advanced, 600 DPI로 스캔한 TIFF, `C:\ocr` 프로젝트.

**원본 TIFF → Advanced 보정 → 600 DPI 보정 TIFF → DPI 자동 적용 PDF → OCR → 굿노트 확인** 순서다.
Advanced는 페이지 보정을 담당하고, PDF 합성과 OCR은 이 프로젝트가 담당한다.
[Advanced 프로젝트](https://github.com/ScanTailor-Advanced/scantailor-advanced)

## 사용 버전과 DPI 설정 위치

Windows 배포 파일이 있는 [v1.1.1 릴리스](https://github.com/ScanTailor-Advanced/scantailor-advanced/releases/tag/v1.1.1)의
`ScanTailor-Advanced-1.1.1x64.zip`을 문서 기준으로 사용한다.
아래 메뉴 설명은 Windows용 v1.1.1 기준이다. 다른 빌드는 운영체제 지원과 메뉴를 확인한다.

| 확인할 값 | 설정 위치 | 이 작업의 값 |
|---|---|---|
| 원본 입력 DPI | Tools → Fix DPI… → All Pages | 600 × 600 |
| TIFF 출력 DPI | Output → Output Resolution (DPI) → Change… | 600, All pages |
| PDF 변환 DPI | 출력 TIFF의 X/Y 해상도 태그 자동 읽기 | 기본 명령에서 `--dpi` 생략 |

입력 DPI는 스캔 픽셀의 실제 크기를 해석하는 값이고, 출력 DPI는 보정 결과를 생성하는 해상도다.
입력이 600이라고 출력도 자동으로 600이 된다고 가정하지 않는다.
아래 영어 메뉴 이름과 TIFF 저장 동작은 v1.1.1 소스로 확인했으며, Windows 화면을 직접 조작한 검증은 아니다.
[입력 DPI 메뉴](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/app/MainWindow.ui),
[출력 DPI 패널](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/core/filters/output/OptionsWidget.ui),
[출력 적용 창](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/core/filters/output/ChangeDpiDialog.ui),
[TIFF 저장 코드](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/core/TiffWriter.cpp)

Experimental에서 옮겨오는 새 작업은 원본 `raw`로 Advanced 프로젝트를 만들고 별도 출력 폴더에 저장한다.
기존 프로젝트와 보정 TIFF는 보존한다. 이전 96 DPI 출력의 처리 방법은 [TIFF DPI 안내](TIFF_DPI.md#기존-experimental-출력의-예외)에 있다.

## 1. 책마다 원본과 보정본 폴더 나누기

예시에서는 새 작업 이름을 `book_v1`로 사용한다. 실제 책 이름으로 바꿔도 된다.
이미 같은 이름의 PDF나 결과가 있으면 `book_v2`처럼 새 이름을 사용한다.

```text
C:\ocr\
  scans\book_v1\raw\          스캐너에서 받은 원본 TIFF
  scans\book_v1\processed\    ScanTailor가 출력하는 보정 TIFF
  scans\book_v1\              ScanTailor 프로젝트 저장 위치
  input\book_v1.pdf            보정 TIFF를 합친 OCR 입력 PDF
  output\book_v1\             OCR 완성본과 내부 캐시
```

PowerShell에서 필요한 폴더를 준비한다. 원본 TIFF는 `raw`에 복사해 보관한다.

```powershell
Set-Location C:\ocr
New-Item -ItemType Directory -Force -Path "scans/book_v1/raw", "scans/book_v1/processed", "input" | Out-Null
```

한 책의 페이지 순서가 파일명 순서와 일치하도록 `0001.tif`, `0002.tif`, …처럼
자릿수를 맞춘다. 변환기는 이름순으로 읽으므로 `1.tif`, `10.tif`, `2.tif`는 그 순서로 합쳐진다.
ScanTailor에서 좌우 페이지를 분리한 경우에도 **출력 파일의 이름순이 실제 읽기 순서와 맞는지** 확인한다.
프로젝트에 연결한 원본 파일 이름·위치를 나중에 바꾸면 프로젝트 연결도 재설정해야 한다.

## 2. 원본 DPI를 확인하고 Advanced 프로젝트 만들기

1. Advanced에서 **File → New Project…**를 연다.
2. 입력 폴더를 `C:\ocr\scans\book_v1\raw`, 출력 폴더를 `C:\ocr\scans\book_v1\processed`로 지정한다.
3. 처리할 TIFF를 추가하고 순서와 누락을 확인한다.
4. 스캐너 설정과 원본 TIFF 태그를 확인한다. 아래 4단계 검사 코드의 폴더를 `raw`로 바꾸면 원본 태그를 읽을 수 있다.
5. **Tools → Fix DPI…**를 열어 **All Pages** 탭에서 페이지들의 입력 DPI를 확인한다.
   모두 실제 600 DPI 스캔인데 값이 다르다면 대상 페이지 또는 전체 그룹을 선택하고
   **600 × 600 → Apply**를 적용한다. 프로젝트 생성 중 DPI 확인 창이 뜨면 같은 기준으로 처리한다.
6. 프로젝트를 `C:\ocr\scans\book_v1` 아래에 저장한다.

`Need Fixing` 목록이 비어 있어도 실제 스캔 설정과 맞는지는 `All Pages`에서 확인한다.
Fix DPI는 프로젝트에서 원본 해상도를 해석하는 설정이며 원본 TIFF 파일을 다시 스캔하는 기능이 아니다.
원본 파일의 잘못된 태그까지 수정됐다고 가정하지 않는다.
[Fix DPI 구현](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/app/FixDpiDialog.cpp)

이미 다른 프로그램에서 픽셀 수를 변경한 파일이나 해상도가 섞인 책에는 600을 일괄 강제하지 않는다.
이 안내는 스캐너에서 받은 600 DPI 원본으로 시작하는 경우다.

## 3. 여섯 보정 단계 진행하기

대표 페이지에서 설정을 확인한 뒤 필요한 범위에 적용하고, 각 단계의 일괄 처리 버튼으로 진행한다.
방향이나 구성 등이 서로 다른 페이지는 같은 설정을 무조건 전체 적용하지 않는다.

| 단계 | 작업 | 교재에서 확인할 부분 |
|---|---|---|
| Fix Orientation | 페이지 방향 맞추기 | 거꾸로 된 페이지, 가로 도표 |
| Split Pages | 한 장에 두 페이지면 분리하기 | 좌우 순서, 가운데 글자 잘림; 한 페이지 스캔은 불필요하게 분리하지 않기 |
| Deskew | 기울어진 글줄 바로잡기 | 표·그림 때문에 잘못 기울어진 페이지 |
| Select Content | 보존할 내용 영역 지정하기 | 쪽수·머리말·각주·수식·바깥쪽 주석까지 포함 |
| Margins / Page Layout | 여백과 페이지 배치 정하기 | 본문이 잘리지 않는 여백, 같은 책의 과도한 크기 차이 |
| Output | 보정 TIFF 실제 생성하기 | 출력 600 DPI·색상 모드·최종 페이지 수 |

Select Content는 다음 여백·배치 단계에서 사용할 내용 영역이다. OCR할 문장만 지정하는 기능이 아니다.
본문뿐 아니라 필요한 쪽수·머리말·각주·수식·그림까지 포함한다. 자동 감지가 놓친 부분은
영역 경계를 조정하고, Output 결과에서 잘리거나 배경으로 지워진 내용이 없는지 확인한다.

### Output에서 600 DPI 설정하기

1. 왼쪽 단계에서 **Output**을 선택한다.
2. **Output Resolution (DPI)** 패널을 펼치고 **Change…**를 누른다.
3. **DPI: 600**을 선택한다.
4. **Apply to → All pages**를 선택하고 확인한다.
5. 출력 해상도 표시가 600인지 확인한 뒤 Output을 전체 일괄 처리한다.

입력 600 DPI와 출력 600 DPI를 각각 확인한다. 출력 DPI를 변경한 뒤에는 TIFF를 다시 생성한다.
Advanced의 DPI 설정을 Experimental의 Resolution Enhancement / 1x 배율 설정과 혼동하지 않는다.
Advanced는 보정 이미지에 기록된 해상도를 TIFF에 저장하므로, 4단계에서 600 DPI 태그를 확인하고
5단계에서는 자동 읽기를 사용한다.

출력을 300 또는 1200 DPI로 의도적으로 바꾸었다면 PDF도 그 출력 태그를 읽어야 한다.
600 DPI로 스캔했다는 이유로 PDF 변환기에 600을 강제하면 페이지 크기가 틀어질 수 있다.
잘라내기와 여백 변경은 최종 판형을 바꿀 수 있으므로 PDF 물리 크기도 확인한다.

| 문서 내용 | 이 작업에서의 시작 설정 |
|---|---|
| 컬러 그림·그래프·색상 구분이 있는 교재 | Color / Grayscale로 시작하고 색과 옅은 선이 유지되는지 확인 |
| 사진·명암이 필요한 흑백 교재 | Grayscale 계열로 명암 보존 |
| 흰 종이에 검은 글자 위주 | Black and White를 표본에 적용한 뒤 작은 글자·부호 비교 |
| 본문과 그림의 처리를 나누려는 경우 | Mixed를 표본에 적용하고 그림 영역의 자동 구분을 검토 |

처음부터 강한 잡티 제거, 글자 두께 조정, 선명화 등을 전체 적용하지 않는다.
소수점·콜론·괄호·아래첨자·얇은 그래프 선이 사라지는지 확대해서 비교한다.
평평한 스캔에는 굽힘 보정을 불필요하게 추가하지 않고, 휘어진 글줄이 있을 때 표본으로 효과를 확인한다.
이는 수식·코드가 있는 교재의 내용을 보존하기 위한 작업 권고다.

Output의 일괄 처리를 **모든 페이지가 끝날 때까지** 실행한다.
프로젝트를 저장하는 것과 TIFF를 출력하는 것은 다르다. 실제 이미지 파일은 Output에서 생성된다.
앞 단계 설정을 바꾸면 영향을 받는 페이지의 후속 단계와 Output도 다시 처리한다.
[Advanced 기능 설명](https://github.com/ScanTailor-Advanced/scantailor-advanced#features)

## 4. 보정 TIFF 검사하기

`processed` 폴더에는 이번 책의 최종 페이지 TIFF만 둔다. 원본·이전 버전·중간 결과를 섞지 않는다.
페이지를 삭제하거나 분할 설정을 바꾼 뒤 다시 출력할 때는 새 출력 폴더를 쓰면
예전 출력 파일이 섞여 중복 페이지가 되는 것을 피할 수 있다. 기존 폴더는 보존한다.

- 표지·목차·본문·표·수식·마지막 페이지를 열어 글자 잘림과 방향을 확인한다.
- 전체 페이지 목록에서 누락·중복·좌우 순서를 확인한다. 두 페이지 스캔을 분리했으면
  출력 페이지 수가 입력 파일 수보다 많아질 수 있다.
- 출력 TIFF의 DPI를 확인한다. 아래 코드는 픽셀을 바꾸지 않고 모든 프레임의 원시 태그를 읽는다.

최초 준비 시 Python 패키지를 설치한다. 이미 준비된 환경에서는 생략한다.

```powershell
python -m pip install -r requirements.txt
```

```powershell
@'
from pathlib import Path
from PIL import Image, ImageSequence

folder = Path('scans/book_v1/processed')
files = sorted(p for p in folder.iterdir() if p.suffix.lower() in ('.tif', '.tiff'))
if not files:
    raise SystemExit('No TIFF files found')
count = 0
for path in files:
    with Image.open(path) as image:
        for number, frame in enumerate(ImageSequence.Iterator(image), 1):
            tags = frame.tag_v2
            x, y, unit = tags.get(282), tags.get(283), tags.get(296, 2)
            dpi = None
            if x is not None and y is not None and unit in (2, 3):
                factor = 2.54 if unit == 3 else 1
                dpi = (float(x) * factor, float(y) * factor)
            print(path.name, 'frame', number, 'pixels', frame.size, 'dpi', dpi)
            count += 1
print('TIFF pages:', count)
'@ | python -
```

이 작업의 출력은 모든 페이지에서 `(600.0, 600.0)` 또는 반올림 오차 수준의 값이어야 한다.
`96`, `None`, 예상하지 않은 300/1200이 나오면 Output의 적용 범위·재출력 여부와 입력 폴더를 확인한다.
이전 Experimental 출력이나 다른 작업 파일이 섞이지 않았는지도 확인한다.
의도한 출력 DPI와 맞지 않으면 OCR 전에 원인을 해결한다. 태그가 존재한다는 사실만으로
그 값이 실제 해상도와 맞는다고 보장되지는 않는다.

## 5. 보정 TIFF를 PDF로 합치기

Advanced의 기본 경로는 **출력 TIFF의 DPI 자동 읽기 + 무손실 이미지 저장**이다.
입력은 `raw`가 아닌 ScanTailor가 출력한 `processed` 폴더다.

```powershell
python tools/tif_to_pdf.py "scans/book_v1/processed" "input/book_v1.pdf" --lossless
```

변환기는 바로 아래의 `.tif`·`.tiff` 파일을 이름순으로 읽으며 하위 폴더를 재귀 검색하지 않는다.
다중 페이지 TIFF는 파일 안의 프레임 순서를 유지한다. 출력 폴더 `input`은 먼저 만들어 둔다.

`--lossless`는 ScanTailor **보정 결과**의 픽셀을 추가 JPEG 손실 없이 PDF에 저장한다.
ScanTailor의 자르기·기울기 보정·이진화 자체를 되돌리는 의미는 아니다.
현재 변환기는 1비트/팔레트 이미지를 RGB로 바꿀 수 있어 TIFF보다 PDF가 커질 수 있다.

무손실 경로는 페이지마다 압축한 뒤 진행 수를 갱신하고, 마지막에 `PDF 저장 중`과
`PDF 저장 완료`, 저장 후 검사 상태를 표시한다. 페이지 처리 수가 끝나도 저장·검사 완료까지 기다린다.
이전 코드에서 `596/596 페이지` 같은 표시가 오래 유지됐다면 전체 이미지를 마지막에 압축·저장하는
단계일 수 있다. 출력 파일 크기와 프로세스 CPU 사용이 증가하면 아직 작업 중이다.
이전 방식은 수백 장의 RGB 이미지를 압축 없이 메모리에 쌓으므로 두 책 동시 변환 시 특히 무거웠다.
실행 중인 프로세스에는 코드 수정이 적용되지 않는다. 완료 메시지와 프롬프트 복귀를 확인한 뒤 OCR한다.

### 병렬 변환

기본 명령도 **최대 4개 작업자 자동 선택**(`--workers 0`)으로 실행된다. 작업자는 서로 독립된
프로세스에서 TIFF 프레임을 읽고 한 페이지를 압축한다. 주 프로세스가 압축 결과를 원래
파일명·프레임 순서대로 조립하고 최종 PDF를 저장한다. PDF 객체를 스레드 사이에 공유하지 않는다.

```powershell
python tools/tif_to_pdf.py "scans/book_v1/processed" "input/book_v1_parallel.pdf" --lossless --workers 4 --memory-mb 4096
```

- `--workers 0`: 자동, 최대 4개. `--workers 1`: 기존 순차 방식. 2 이상은 요청할 작업자 수의 상한이다.
- `--memory-mb 4096`: 작업자들의 합산 메모리 **추정 예산**. 시작 시 페이지 크기와 가용 RAM을
  함께 보고 실제 작업자 수를 낮춘다. 한 페이지 처리에도 예산이 부족하면 순차 방식으로 실행한다.
- 대기 중인 작업과 아직 조립하지 않은 결과는 실제 작업자 수 이하로 제한한다.
- 메모리 값은 OS가 강제하는 한도가 아니며, 주 프로세스의 누적된 압축 PDF는 별도로 메모리를 쓴다.
  두 책을 동시에 실행하면 각각 예산이 잡히므로 우선 한 책씩 처리하거나 각 `--workers`를 줄인다.
- 프로세스 생성 실패나 작업자 종료 시 이미 조립한 페이지를 유지하고 남은 페이지를 순차 처리한다.
  손상 TIFF처럼 순차 처리도 불가능한 오류는 보고하고 중단한다.

순차 처리로 실행하려면 다음 명령을 쓴다.

```powershell
python tools/tif_to_pdf.py "scans/book_v1/processed" "input/book_v1_serial.pdf" --lossless --workers 1
```

용량을 줄이는 것이 더 중요하면 위 명령 **대신** 다음 명령으로 별도 PDF를 만든다.
JPEG 압축 때문에 미세한 픽셀 차이가 생기므로 작은 글자와 수식을 비교한다.

```powershell
python tools/tif_to_pdf.py "scans/book_v1/processed" "input/book_v1_jpeg.pdf" --quality 95
```

품질 기본값은 88이다. `--lossless`를 쓸 때 `--quality`는 사용되지 않는다.
각 축 DPI와 JPEG 메타데이터 처리의 상세는 [TIFF DPI 안내](TIFF_DPI.md)에 있다.

### DPI 경고가 나거나 태그가 잘못됐을 때

변환기는 해상도가 없거나 유효하지 않으면 경고 후 600 DPI를 가정한다. 이는 실제 측정값이 아니다.
Advanced에서는 먼저 입력 DPI와 출력 DPI를 확인하고 새 폴더에 TIFF를 다시 생성하는 방법을 권한다.

별도로 확인한 실제 출력 해상도가 600 DPI이고 태그만 잘못된 파일에 한해 다음처럼 강제할 수 있다.

```powershell
python tools/tif_to_pdf.py "scans/book_v1/processed" "input/book_v1_dpi600.pdf" --lossless --dpi 600
```

`--dpi`는 픽셀 수를 바꾸지 않고 PDF 물리 크기 계산을 덮어쓴다. 정상 Advanced 출력에는 필요 없다.
96 등 형식상 유효한 잘못된 값에는 변환기가 경고하지 않으므로 앞 단계의 태그 검사를 생략하지 않는다.

## 6. OCR 전에 PDF 크기와 순서 확인하기

아래는 기본 명령으로 만든 `input/book_v1.pdf`의 모든 페이지 크기를 집계하는 읽기 전용 검사다.
다른 이름으로 만들었다면 경로를 바꾼다.

```powershell
@'
from collections import Counter
import pymupdf

with pymupdf.open('input/book_v1.pdf') as doc:
    sizes = Counter((round(p.rect.width * 25.4 / 72, 2),
                     round(p.rect.height * 25.4 / 72, 2)) for p in doc)
    print('PDF pages:', len(doc))
    for (width, height), count in sizes.items():
        print(f'{width} x {height} mm: {count} pages')
'@ | python -
```

PDF 페이지 수는 4단계에서 센 TIFF 프레임 수와 같아야 한다.
PDF 뷰어에서 실제 페이지 순서와 잘림도 확인한다. 이 시점의 PDF는 아직 OCR 텍스트가 없는 이미지 PDF다.

페이지 크기는 책의 판형에 보정된 자르기·여백을 반영한 값이어야 한다. 무조건 A4로 맞추지 않는다.
예를 들어 4462 × 5753픽셀 이미지는 600 DPI에서 약 **188.9 × 243.5mm**다.
같은 이미지가 96 DPI로 배치되면 약 **1180.6 × 1522.1mm**가 된다.
책 한 페이지가 1m 이상으로 나타나면 OCR 전에 DPI와 배율을 바로잡는다.

## 7. OCR 실행하고 완료 확인하기

실제 OCR에는 GPU, Docker Desktop, OCR 이미지와 모델 준비가 필요하다.
서버가 꺼져 있으면 CLI가 준비를 시도하지만, 이미지·모델 설치를 자동으로 대신하지는 않는다.
초기 환경 설정은 [설치 안내](SETUP.md)를 참고한다.

```powershell
python run.py "input/book_v1.pdf" --out "output/book_v1"
```

인쇄 목차를 바탕으로 PDF 책갈피도 생성하려면 위 명령 **대신** 다음을 사용한다.

```powershell
python run.py "input/book_v1.pdf" --out "output/book_v1" --bookmarks
```

자동 책갈피는 인쇄 목차와 본문을 대조하는 선택 기능이며, 결과 검토가 필요하다.
[책갈피 안내](BOOKMARKS.md)를 참고한다. 완성본에는 기본 정밀 자동 모드를 사용한다.
`--fast`는 자동 최종 검증을 생략하므로 이 작업 순서의 기본 명령에 넣지 않는다.

주요 결과 위치:

```text
C:\ocr\output\book_v1\book_v1_auto_searchable.pdf
C:\ocr\output\book_v1\.ocr\book_v1_line_ocr.sqlite3
```

첫 파일이 굿노트에 가져갈 완성본이다. DB에는 재개 캐시와 보고서가 있다.
같은 입력 PDF·같은 명령으로 중단된 작업을 재개한다. 실행 중 입력 PDF를 다시 만들거나 교체하지 않는다.
ScanTailor 보정을 바꿨다면 새 작업 이름과 출력 폴더로 변환해 기존 결과·캐시와 구분한다.

OCR의 픽셀 보존 기준은 **5단계에서 만든 입력 PDF**다. 원시 TIFF에서 ScanTailor가 변경한
부분까지 같다고 보장하는 검사는 아니다. 자동 검증이 통과해도 OCR 문구의 정답률을 보장하지 않는다.

## 8. 보고서와 굿노트에서 최종 확인하기

품질 신호와 검토할 페이지를 확인한다.

```powershell
python run.py audit "output/book_v1/book_v1_auto_searchable.pdf"
```

보고서를 파일로 보고 싶으면 DB 내용을 새 폴더로 내보낸다.

```powershell
python run.py export "output/book_v1/.ocr/book_v1_line_ocr.sqlite3" "output/book_v1/reports_export"
```

내보낸 `book_v1_auto_report.md`와 상태 기록을 확인한다.
`completed_with_warnings`는 완료됐지만 원본과 대조할 항목이 있다는 뜻이다.
`audit`는 품질 신호이며, 정답 텍스트와 대조한 정확도 점수는 아니다.

1. PC의 PDF 뷰어에서 눈에 보이는 한글 문장을 선택·복사하고 붙여넣어 확인한다.
2. 표·수식·코드 등 까다로운 페이지와 경고 페이지를 입력 PDF와 대조한다.
3. 굿노트에 `book_v1_auto_searchable.pdf`를 새 문서로 가져온다. `input/book_v1.pdf`와 혼동하지 않는다.
4. 색인 완료를 기다린 뒤, 확인한 문장의 단어를 검색하고 같은 위치의 텍스트 선택도 검사한다.

Goodnotes는 가져온 PDF에 이미 있는 OCR 텍스트를 사용한다.
검색 실패 시 다른 뷰어의 선택·복사 결과와 색인 상태를 함께 확인한다.
정상 DPI만으로 모든 굿노트 호환성이 보장되지는 않는다.
[Goodnotes 공식 검색 문제 안내](https://support.goodnotes.com/hc/en-us/articles/7353695209743-I-can-t-search-my-notes)

## 자주 헷갈리는 점

| 상황 | 처리 |
|---|---|
| DPI 설정이 안 보인다 | Advanced인지 확인한다. 입력은 Tools → Fix DPI…, 출력은 Output → Output Resolution (DPI) 패널을 펼쳐 Change…로 설정한다. |
| Fix DPI를 600으로 했는데 출력은 다르다 | 입력 DPI와 출력 DPI는 별도다. Output에서도 600을 All pages에 적용하고 다시 출력한다. |
| 보정 TIFF가 96 DPI로 나온다 | 이 안내의 Advanced 600 DPI 출력과 맞지 않는다. 실행 버전·출력 설정·재생성 여부·이전 출력 혼입을 확인한다. |
| TIFF를 그냥 `run.py`에 넘기고 싶다 | 먼저 `tools/tif_to_pdf.py`로 PDF를 만든다. `run.py`의 폴더 입력은 PDF 배치용이다. |
| 페이지 크기는 정상인데 순서가 틀렸다 | 보정 TIFF의 파일명 순서와 다중 프레임 순서를 확인한다. |
| 일부 페이지 크기만 다르다 | 입력 DPI, Select Content, 여백·배치, 출력 DPI의 적용 범위를 확인한다. |
| 예전에 만든 큰 PDF가 있다 | 새 TIFF 변환만 개선된다. 기존 PDF를 그대로 OCR해도 페이지 크기는 자동 수정되지 않는다. |
| PDF 용량이 크다 | 원본·무손실본은 보존하고 JPEG 버전을 별도로 비교한다. DPI 숫자만 바꾸는 것은 이미지 압축이 아니다. |
