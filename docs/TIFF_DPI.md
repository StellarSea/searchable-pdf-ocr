# TIFF 원본 해상도 보존

`tools/tif_to_pdf.py`는 TIFF를 OCR 입력 PDF로 만드는 독립 도구다.
`run.py`의 PDF OCR은 입력 페이지 크기를 그대로 보존한다.

ScanTailor Advanced를 쓰는 실제 작업 순서는
[ScanTailor Advanced에서 OCR PDF까지](SCANTAILOR_WORKFLOW.md)를 참고한다.
자동으로 적용되는 DPI는 변환기에 넘긴 TIFF 태그의 DPI다. 태그가 실제 물리 해상도를
정확히 나타내는 경우에만 원본 크기를 복원한다.

Advanced에서는 **입력 DPI와 출력 DPI를 각각 확인**한다. 600 DPI 스캔이면
Tools → Fix DPI에서 입력 600 × 600을 확인하고, Output → Output Resolution (DPI)
→ Change…에서 600을 전체 페이지에 적용한 뒤 TIFF를 생성한다.
Advanced의 [TIFF 저장 코드](https://github.com/ScanTailor-Advanced/scantailor-advanced/blob/v1.1.1/src/core/TiffWriter.cpp)는
출력 이미지의 해상도와 물리 단위를 기록한다. 기본 변환은 이 태그를 자동으로 읽으므로
`--dpi`를 생략한다. 출력 300/1200 DPI를 선택했다면 해당 출력 값을 읽는 것이 맞다.

```powershell
python tools/tif_to_pdf.py "scans/book_v1/processed" "input/book.pdf" --lossless
python tools/tif_to_pdf.py "scan.tif" "input/book.pdf" --lossless
```

태그 오류가 있고 실제 출력 해상도가 600 DPI임을 별도로 확인했을 때만 강제 지정한다.
이 옵션은 픽셀을 확대·축소하지 않고 PDF의 물리 크기를 정한다.

```powershell
python tools/tif_to_pdf.py "scan.tif" "input/book.pdf" --dpi 600
```

- TIFF 각 프레임의 XResolution(282), YResolution(283), ResolutionUnit(296)을
  색상 변환 전에 읽는다. inch는 그대로, cm는 2.54를 곱해 DPI로 환산한다.
  단위 태그 생략 시 TIFF 기본값인 inch를 적용한다.
- 가로·세로 크기를 각각 `픽셀 수 / 해당 축 DPI * 72` point로 계산한다.
  축별 DPI가 달라도 이미지가 페이지 전체를 채우며 픽셀 수는 바뀌지 않는다.
- JPEG에도 원본 DPI를 전달한다. JPEG JFIF에는 정수 DPI가 기록되므로 소수는
  반올림되지만 PDF 페이지 크기는 원본의 소수 DPI로 계산한다.
- `--dpi`는 두 축 모두에 우선 적용한다. 0, 음수, NaN, 무한대는 거부한다.
- 해상도 누락·잘못된 값·물리 단위 없음은 경고 후 기존 기본값인 600 DPI를 쓴다.
  이는 측정한 원본 DPI가 아니라 가정이다. 실제 스캔 설정에 맞춰 `--dpi`로 지정한다.
  Pillow가 태그 없는 TIFF에 합성할 수 있는 `(1, 1)`은 원본 해상도로 오인하지 않는다.
- 기본 JPEG 품질 88과 `--lossless`는 유지한다. JPEG 경로는 기존처럼 손실 압축이고,
  `--lossless`는 RGB/회색조로 변환된 픽셀을 그대로 저장한다.
  무손실 이미지는 페이지마다 Flate 압축해 수백 페이지의 비압축 픽셀이 메모리에 누적되는 것을 줄인다.
  마지막 파일 저장과 저장 후 검사 상태도 별도로 표시한다. 압축된 책 데이터는 여전히 메모리에 유지한다.

CLI의 기본값 `--workers 0`은 최대 4개 독립 프로세스로 TIFF 읽기·페이지 압축을 수행한다.
부모 프로세스는 압축된 한 페이지 PDF만 받아 입력 순서대로 조립하며 최종 파일을 단독 저장한다.
JPEG 경로도 같은 병렬 방식을 사용할 수 있다. DPI 추출·색 변환·압축 품질은 순차 방식과 같다.
`--workers 1`로 순차 처리한다. Python API `convert_tiffs()`는 기존 호환성을 위해 기본 순차 처리이며,
병렬 사용 시 `workers=0` 또는 2 이상을 명시한다. Windows 호출 스크립트는 `if __name__ == '__main__':` 보호가 필요하다.
`--memory-mb` 기본값은 4096 MiB다. 페이지 헤더로 계산한 작업자 메모리와 시작 시 가용 RAM에 따라
작업자 수를 줄이며, 대기/완료 결과 수는 실제 작업자 수 이하로 유지한다. OS의 강제 메모리 한도는 아니다.

DPI 옵션은 새 TIFF 변환에 적용된다. 이미 만들어진 PDF, OCR 결과, 캐시를 변경하거나
굿노트 호환성을 자동으로 해결하지 않는다. 원본 이미지에 물리 DPI 정보가 없으면
픽셀만으로 실제 스캔 DPI를 복원할 수 없다.

## 기존 Experimental 출력의 예외

ScanTailor Experimental 1.2026.08.08의
[TIFF 저장 코드](https://github.com/ImageProcessing-ElectronicPublications/scantailor-experimental/blob/1.2026.08.08/src/TiffWriter.cpp)는
X/Y 해상도를 96으로 기록한다. 96은 형식상 유효하므로 자동 읽기가 오류를 감지하지 못한다.
600 DPI 원본을 Output 1x 및 추가 확대·축소 없이 처리했다면 PDF 합성에 `--dpi 600`을 지정한다.
다른 원본 DPI나 배율은 그에 맞는 유효 DPI를 사용한다. 출력 태그만으로 실제 원본 DPI를 추정하지 않는다.

새 Advanced 작업은 원본 `raw`에서 시작하고 별도 프로젝트·출력 폴더를 사용한다.

검증: `python -m unittest discover -s tests -p test_tif_to_pdf.py -v`.
임시 TIFF/PDF로 600 DPI, 다중 프레임, 축별 DPI, cm 단위, 팔레트 변환,
JPEG 메타데이터, 무손실 픽셀/렌더링, 누락/잘못된 DPI 및 CLI 오류를 검사한다.
저장 전 페이지별 압축과 반복 이미지·회색조의 픽셀/페이지 순서도 검사한다.
Windows spawn을 사용하는 실제 병렬 테스트로 순차/병렬의 픽셀·렌더링·DPI·JPEG 바이트를 비교한다.
작업 완료 순서가 바뀐 경우의 조립 순서, 메모리 예산에 따른 작업자 축소, 풀 시작/작업자 실패의 순차 복구도 검사한다.
