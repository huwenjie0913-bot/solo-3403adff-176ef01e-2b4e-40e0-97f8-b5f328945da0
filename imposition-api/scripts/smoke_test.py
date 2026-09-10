"""端到端冒烟测试：不走网络，直接用 FastAPI TestClient 调三个接口。

用法: python3 scripts/smoke_test.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from scripts.make_sample import make_sample  # noqa: E402

SPEC_BASE = {
    "finished_width": 105, "finished_height": 148,
    "sheet_width": 320, "sheet_height": 450,
    "grain": "long_edge", "bleed": 3, "gripper": 10,
    "gripper_edge": "bottom", "flip": "long_edge",
}

client = TestClient(app)
failures = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def post(path: str, pdf_path: str, spec: dict, extra: dict | None = None):
    data = {"spec": json.dumps(spec)}
    if extra:
        data.update({k: str(v) for k, v in extra.items() if v is not None})
    with open(pdf_path, "rb") as f:
        return client.post(path, files={"file": ("src.pdf", f, "application/pdf")}, data=data)


print("== 1. 骑马订 32 页 ==")
src = make_sample(32, out="/tmp/sample32.pdf")
spec = {**SPEC_BASE, "binding": "saddle"}
r = post("/api/plans", src, spec)
check("plans 200", r.status_code == 200, r.text[:300])
body = r.json()
check("有可行方案", len(body["plans"]) > 0)
best = body["plans"][0]
print(f"  最优方案: {best['id']} 每面{best['pages_per_side']}页 "
      f"用纸{best['sheets_total']}张 空白{best['blank_pages']}页 每纸{best['cuts_per_sheet']}刀")
check("骑马订32页用纸4张", best["sheets_total"] == 4)
check("无空白页", best["blank_pages"] == 0)
check("页码网格完整", len(best["sheets"][0]["front"]["cells"]) == best["pages_per_side"])
front_pages = sorted(c["page"] for c in best["sheets"][0]["front"]["cells"])
check("第1张正面含第1页和最后页", 1 in front_pages and 32 in front_pages, str(front_pages))
check("有裁切折叠步骤", len(best["steps"]) >= 3)

r = post("/api/ticket", src, spec)
check("ticket 200", r.status_code == 200, r.text[:300])
check("工单含 sheets/steps", "sheets" in r.json() and "steps" in r.json())

r = post("/api/pdf", src, spec)
check("pdf 200", r.status_code == 200, r.text[:300])
check("返回 PDF", r.content[:5] == b"%PDF-")
from pypdf import PdfReader  # noqa: E402
import io  # noqa: E402
out = PdfReader(io.BytesIO(r.content))
check("PDF 页数 = 用纸×2", len(out.pages) == best["sheets_total"] * 2, str(len(out.pages)))
w_pt = float(out.pages[0].mediabox.width)
check("PDF 页面=纸张尺寸", abs(w_pt - 320 * 72 / 25.4) < 1, f"{w_pt:.1f}pt")
open("/tmp/imposed_saddle.pdf", "wb").write(r.content)

print("== 2. 胶装 40 页 / 每帖16页 ==")
src40 = make_sample(40, out="/tmp/sample40.pdf")
spec_p = {**SPEC_BASE, "binding": "perfect", "pages_per_signature": 16}
r = post("/api/plans", src40, spec_p)
check("plans 200", r.status_code == 200, r.text[:300])
body = r.json()
check("有可行方案", len(body["plans"]) > 0)
best_p = body["plans"][0]
print(f"  最优方案: {best_p['id']} 每面{best_p['pages_per_side']}页 "
      f"{best_p['signatures']}帖×{best_p['sheets_per_signature']}张 空白{best_p['blank_pages']}页")
check("胶装容量为16的倍数", best_p["capacity_pages"] % 16 == 0)
check("空白页=容量-40", best_p["blank_pages"] == best_p["capacity_pages"] - 40)
r = post("/api/pdf", src40, spec_p)
check("胶装 pdf 200", r.status_code == 200, r.text[:300])
open("/tmp/imposed_perfect.pdf", "wb").write(r.content)

print("== 3. 无解场景 ==")
# 纹向错误：短边纹纸 → 正放被否决（只剩横放方案），并给出纹向原因
bad_grain = {**SPEC_BASE, "binding": "saddle", "grain": "short_edge"}
r = post("/api/plans", src, bad_grain)
body = r.json()
check("纹向不符时正放被否决", all(p["rotation"] == 90 for p in body["plans"])
      and len(body["rejections"]) > 0)
if body["rejections"]:
    print(f"  原因示例: {body['rejections'][0]['reason']}")
check("原因提及纹向/书脊", "纹向" in body["rejections"][0]["reason"])

# 纸张太小
tiny = {**SPEC_BASE, "binding": "saddle", "sheet_width": 150, "sheet_height": 200}
r = post("/api/plans", src, tiny)
body = r.json()
check("纸小无解有原因", len(body["plans"]) == 0 and len(body["rejections"]) > 0)
if body["rejections"]:
    print(f"  原因示例: {body['rejections'][0]['reason']}")

# 源文件尺寸错误
src_bad = make_sample(8, 100, 140, "/tmp/sample_bad.pdf")
r = post("/api/plans", src_bad, spec)
check("尺寸不符返回 422", r.status_code == 422)
check("错误信息含尺寸", "尺寸" in r.text or "mm" in r.text)

# 胶装每帖页数与开数不匹配 → 该方案被否决并给出建议
mismatch = {**SPEC_BASE, "binding": "perfect", "pages_per_signature": 12}
r = post("/api/plans", src40, mismatch)
body = r.json()
rej_text = json.dumps(body["rejections"], ensure_ascii=False)
check("帖页数不匹配有原因", len(body["rejections"]) > 0)
check("原因含调整建议", "每帖" in rej_text)

print("== 4. 指定开数选择器 ==")
r = post("/api/ticket", src, spec, {"rotation": 0, "cols": 2, "rows": 2})
check("指定 2x2 方案", r.status_code == 200 and r.json()["plan"]["cols"] == 2, r.text[:200])
r = post("/api/ticket", src, spec, {"rotation": 0, "cols": 8, "rows": 8})
check("不存在的方案返回 404", r.status_code == 404)

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过 ✔  样例输出: /tmp/imposed_saddle.pdf, /tmp/imposed_perfect.pdf")
