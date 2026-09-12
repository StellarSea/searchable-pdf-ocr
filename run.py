import base64, json, sys, time
from pathlib import Path
import requests

src = Path(sys.argv[1])
out = Path(r"C:\ocr\output") / src.stem
out.mkdir(parents=True, exist_ok=True)

payload = {
    "file": base64.b64encode(src.read_bytes()).decode(),
    "fileType": 0,          # 0 = PDF, 1 = 이미지
    "useChartRecognition": False,
    "useSealRecognition": False,
}

t0 = time.time()
r = requests.post("http://localhost:8080/layout-parsing", json=payload, timeout=None)
r.raise_for_status()
res = r.json()["result"]

n = res["dataInfo"]["numPages"]
print(f"{n} pages in {time.time()-t0:.1f}s  ({(time.time()-t0)/n:.1f}s/page)")

md = []
for i, page in enumerate(res["layoutParsingResults"], 1):
    md.append(page["markdown"]["text"])
    for name, b64 in (page["markdown"].get("images") or {}).items():
        p = out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(b64))

(out / f"{src.stem}.md").write_text("\n\n".join(md), encoding="utf-8")
print("saved ->", out)