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

print("== 5. 回归：裁切/折叠分离 + 出血计入可印区域 ==")
from app.pdf_builder import compute_mark_geometry  # noqa: E402

r = post("/api/ticket", src, spec, {"rotation": 0, "cols": 2, "rows": 2})
tk = r.json()
steps = tk["steps"]
cut_idx = next(i for i, s in enumerate(steps) if s.startswith("裁切"))
fold_idx = next(i for i, s in enumerate(steps) if s.startswith("折叠"))
check("裁切步骤在折叠步骤之前", cut_idx < fold_idx, steps[0][:40])
check("2x2 工单无纵切（中线非裁切线）", "纵切" not in steps[cut_idx], steps[cut_idx])
check("工单声明中线仅为折叠线", "中线仅为折叠线" in steps[cut_idx])
# 2x2、出血3mm：单元间仅 1 条横向间隔，出血>0 每间隔 2 刀 → 共 2 刀
check("2x2 裁切刀数=2（横间隔×2）", tk["plan"]["cuts_per_sheet"] == 2,
      str(tk["plan"]["cuts_per_sheet"]))

# 工单与 PDF 标线一致：同一几何函数，裁切线数==工单刀数，折叠线数==单元数
from app.schemas import CellModel  # noqa: E402
cells = tk["sheets"][0]["front"]["cells"]
geo = compute_mark_geometry([CellModel(**c) for c in cells])
n_cuts = len(geo["cut_v"]) + len(geo["cut_h"])
check("标线裁切数与工单刀数一致", n_cuts == tk["plan"]["cuts_per_sheet"],
      f"{n_cuts} vs {tk['plan']['cuts_per_sheet']}")
check("折叠线数=每行单元列数", len(geo["fold_v"]) == tk["plan"]["cols"] // 2)
check("裁切线与折叠线无交集", not (set(geo["cut_v"]) & set(geo["fold_v"])))
# 裁切线不得穿过任何折页单元内部
units = {}
for c in cells:
    units.setdefault((c["row"], c["col"] // 2), []).append(c["x_mm"])
bad = []
for (row, cp), xs in units.items():
    lo, hi = min(xs), max(xs) + cells[0]["w_mm"]
    for x in geo["cut_v"]:
        if lo + 0.01 < x < hi - 0.01:
            bad.append((row, cp, x))
check("裁切线不穿过折页单元", not bad, str(bad))

# PDF 内容流：裁切虚线与折叠点划线两种线型都在
r = post("/api/pdf", src, spec, {"rotation": 0, "cols": 2, "rows": 2})
check("2x2 pdf 200", r.status_code == 200)
out = PdfReader(io.BytesIO(r.content))
stream = out.pages[0].get_contents().get_data()
check("PDF 含裁切虚线线型", b"[4 3] 0 d" in stream)
check("PDF 含折叠点划线线型", b"[10 3] 0 d" in stream)

# 越界：210×158 纸 + 底部咬口10 + 出血3 → 含出血单元 216×154 超出 210×148 可印区
# （短边纹使纹向校验通过，确保拒绝原因就是尺寸/出血）
over = {**SPEC_BASE, "binding": "saddle", "sheet_width": 210, "sheet_height": 158,
        "grain": "short_edge"}
r = post("/api/plans", src, over)
body = r.json()
check("越界方案被拒绝", len(body["plans"]) == 0 and len(body["rejections"]) > 0)
reasons = json.dumps(body["rejections"], ensure_ascii=False)
check("原因提及出血", "出血" in reasons)
check("原因提及咬口", "咬口" in reasons)
check("首条原因就是越界", "放不下一个折页单元" in body["rejections"][0]["reason"])
print(f"  原因示例: {body['rejections'][0]['reason']}")

# 临界可行：230×170 纸（可印 230×160 ≥ 216×154）应通过
fit = {**over, "sheet_width": 230, "sheet_height": 170}
r = post("/api/plans", src, fit)
check("临界可行方案被接受", len(r.json()["plans"]) > 0)


print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过 ✔  样例输出: /tmp/imposed_saddle.pdf, /tmp/imposed_perfect.pdf")
