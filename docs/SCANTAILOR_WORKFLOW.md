# ScanTailor Advanced에서 OCR PDF까지

Windows용 ScanTailor Advanced로 600 DPI 원본 TIFF를 보정하고, 이 프로젝트에서
PDF 합성·OCR을 수행하는 순서다. 명령은 의존성을 설치한 Python과 저장소 루트 기준이다.
처음 설치는 [SETUP](SETUP.md)을 따른다. `book_v1`은 실제 작업 이름으로 바꾸고,
다시 보정할 때는 새 이름·출력 폴더를 사용해 원본과 이전 결과를 보존한다.

## 1. 폴더와 페이지 순서 준비

```text
scans/book_v1/raw/          스캐너 원본 TIFF
scans/book_v1/processed/    Advanced 출력 TIFF
scans/book_v1/              Advanced 프로젝트
input/book_v1.pdf           OCR 입력 PDF
output/book_v1/             OCR 완성본과 캐시
```

```powershell
New-Item -ItemType Directory -Force -Path "scans/book_v1/raw", "scans/book_v1/processed", "input" | Out-Null
```

원본 TIFF는 `raw`에 보관한다. `0001.tif`, `0002.tif`처럼 파일명 자릿수를 맞춘다.
변환기는 이름순으로 읽으므로 `1.tif`, `10.tif`, `2.tif`는 그 순서로 합쳐진다.
좌우 페이지를 분리했으면 **출력 파일의 이름순도 실제 읽기 순서와 맞는지** 확인한다.

## 2. Advanced의 입력·출력 DPI 설정

메뉴 기준은 공식 [v1.1.1 Windows x64 배포본](https://github.com/ScanTailor-Advanced/scantailor-advanced/releases/tag/v1.1.1)의
`ScanTailor-Advanced-1.1.1x64.zip`이다. 메뉴와 TIFF 저장 동작은 해당 버전 소스로 확인했으며,
Windows UI를 직접 조작한 검증은 아니다. 다른 빌드는 지원 환경과 메뉴를 확인한다.

1. **File → New Project…**에서 입력 `scans/book_v1/raw`, 출력 `scans/book_v1/processed`를 선택한다.
2. TIFF를 추가하고 순서·누락을 확인한다. 프로젝트는 `scans/book_v1/`에 저장한다.
3. **Tools → Fix DPI… → All Pages**에서 입력 DPI를 확인한다. 실제 600 DPI 스캔인 페이지만
   **600 × 600 → Apply**로 맞춘다. `Need Fixing`이 비어 있어도 실제 스캔 설정을 확인한다.
4. 보정 후 **Output → Output Resolution (DPI) → Change…**에서 **600 → All pages**를 적용한다.
5. Output 표시를 확인하고 모든 페이지를 일괄 처리한다. 프로젝트 저장만으로 TIFF가 생성되지는 않는다.

입력 DPI는 원본 픽셀의 물리 크기를 해석하고, 출력 DPI는 보정 이미지를 생성하는 값이다.
Fix DPI가 원본 TIFF 태그까지 고쳤다고 가정하지 않는다. 설정을 바꾼 페이지는 후속 단계와 Output도
다시 처리한다. 300/1200 DPI를 의도적으로 출력했다면 PDF도 그 **출력** 태그를 읽어야 한다.
서로 다른 실제 해상도의 원본에 600을 일괄 강제하지 않는다.

공식 소스: [입력 메뉴](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/app/MainWindow.ui),
[입력 DPI 처리](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/app/FixDpiDialog.cpp),
[출력 패널](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/core/filters/output/OptionsWidget.ui),
[출력 적용 창](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/core/filters/output/ChangeDpiDialog.ui),
[TIFF 저장](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/core/TiffWriter.cpp).
이전 Experimental의 96 DPI 출력은 [TIFF 예외 안내](TIFF_DPI.md#기존-experimental-출력의-예외)를 따른다.

## 3. 보정하면서 내용 보존

대표 페이지에서 설정을 확인한 뒤 필요한 범위에 적용한다.

| 단계 | 확인할 내용 |
|---|---|
| Fix Orientation | 거꾸로 된 페이지와 가로 도표의 방향 |
| Split Pages | 좌우 순서, 가운데 글자 잘림; 한 페이지 스캔은 분리하지 않음 |
| Deskew | 표·그림 때문에 잘못 기울어진 글줄 |
| Select Content | 쪽수·머리말·각주·수식·그림·바깥 주석까지 포함 |
| Margins / Page Layout | 내용이 잘리지 않는 여백과 판형 |
| Output | DPI·색상 모드·최종 페이지 수, 옅은 선과 작은 부호 |

컬러 그림은 Color / Grayscale, 사진과 명암은 Grayscale로 시작한다. Black and White나 Mixed는
표본의 작은 글자·부호·그림 영역을 대조한 뒤 적용한다. 강한 잡티 제거·두께 조정·선명화로
소수점·콜론·괄호·첨자·얇은 선이 사라지지 않는지 확대해서 확인한다. 굽힘 보정도 필요한 페이지에서
효과를 확인한다. Select Content는 OCR 문장만 지정하는 기능이 아니며, 최종 TIFF에서 내용 보존을 검사한다.

## 4. 출력 TIFF 검사

`processed`에는 이번 출력만 둔다. 삭제·분할 설정을 바꿨다면 새 출력 폴더를 사용해 이전 파일의 혼입을
피한다. 표지·목차·본문·표·수식·마지막 페이지의 잘림·방향과 전체 페이지의 누락·중복·좌우 순서를 확인한다.
다음 읽기 전용 명령으로 모든 TIFF 프레임의 원시 해상도 태그를 확인한다.

```powershell
@'
from pathlib import Path
from PIL import Image, ImageSequence

files = sorted(p for p in Path('scans/book_v1/processed').iterdir()
               if p.suffix.lower() in ('.tif', '.tiff'))
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

600 DPI 출력은 `(600.0, 600.0)` 또는 반올림 오차 수준이어야 한다. `96`, `None`, 의도하지 않은
300/1200이면 적용 범위·재출력·이전 파일 혼입을 확인한다. 태그의 존재가 실제 해상도까지 보증하지는 않는다.
원본 태그는 위 경로를 `raw`로 바꿔 확인할 수 있다.

## 5. PDF 합성과 판형·순서 검사

정상 Advanced 출력은 태그를 자동으로 읽고 무손실 저장한다.

```powershell
python tools/tif_to_pdf.py "scans/book_v1/processed" "input/book_v1.pdf" --lossless
```

변환기는 바로 아래 TIFF를 이름순·프레임순으로 조립한다. 하위 폴더는 읽지 않는다.
`--lossless`는 보정 TIFF에 추가 JPEG 손실을 만들지 않으며 ScanTailor의 변형을 되돌리지는 않는다.
저장·검사 완료와 프롬프트 복귀까지 기다린다. 병렬 작업자·메모리·JPEG 및 DPI 강제 옵션은
[TIFF DPI 안내](TIFF_DPI.md)에 모았다. `--dpi`는 픽셀을 바꾸지 않고 PDF 크기를 덮어쓰므로,
태그만 잘못됐고 실제 출력 해상도를 확인한 경우에만 지정한다.

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

PDF 페이지 수는 위 TIFF 프레임 수와 같아야 한다. 뷰어에서 순서·잘림도 확인한다.
판형은 자르기·여백을 반영해야 하며 A4로 강제하지 않는다. 4462 × 5753픽셀은 600 DPI에서
약 188.9 × 243.5 mm지만 96 DPI에서는 약 1180.6 × 1522.1 mm다. 책 한 쪽이 1m 이상이면 DPI부터 확인한다.

## 6. OCR과 결과 확인

[설치·모델·서비스 확인](SETUP.md)을 마친 뒤 실행한다. 책갈피를 원하면 `--bookmarks`를 추가한다.

```powershell
python run.py "input/book_v1.pdf" --out "output/book_v1"
python run.py audit "output/book_v1"
python run.py export "output/book_v1/.ocr/book_v1_line_ocr.sqlite3" "output/book_v1/reports_export"
```

완성본은 `output/book_v1/book_v1_auto_searchable.pdf`, 캐시·보고서는 `.ocr/book_v1_line_ocr.sqlite3`다.
중단되면 같은 명령으로 재개하고 실행 중 입력을 바꾸지 않는다. 최종 검증을 생략하는 `--fast`는
이 작업의 기본 실행에 쓰지 않는다. OCR의 픽셀 보존 기준은 합성한 **입력 PDF**이며 원시 TIFF와의 동일성은 아니다.
검증·audit 통과가 OCR 정답률을 보증하지 않는다. 자세한 경고·재개는 [사용 안내](OCR_USAGE.md)를 따른다.

내보낸 `book_v1_auto_report.md`의 경고 페이지와 표·수식·코드를 원본 PDF와 대조한다.
PC 뷰어에서 한글 선택·복사를 확인하고 완성본을 Goodnotes에 새 문서로 가져온 뒤 색인 완료를 기다린다.
같은 단어의 검색·선택 위치를 확인한다. 검색 실패 시 다른 뷰어의 복사 결과와 색인 상태를 함께 확인한다.
정상 DPI만으로 Goodnotes 호환성을 보증하지는 않는다.
[공식 검색 문제 안내](https://support.goodnotes.com/hc/en-us/articles/7353695209743-I-can-t-search-my-notes).
