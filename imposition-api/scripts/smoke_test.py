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
from app.schemas import CellModel, JobSpec  # noqa: E402

r = post("/api/ticket", src, spec, {"rotation": 0, "cols": 2, "rows": 2})
tk = r.json()
steps = tk["steps"]
cut_idx = next(i for i, s in enumerate(steps) if s.startswith("裁切"))
fold_idx = next(i for i, s in enumerate(steps) if s.startswith("折叠"))
check("裁切步骤在折叠步骤之前", cut_idx < fold_idx, steps[0][:40])
check("工单含 纵切1刀", "纵切 1 刀" in steps[cut_idx], steps[cut_idx][:60])
check("工单含 横切1刀", "横切 1 刀" in steps[cut_idx])
check("工单表达先外部裁切", "先外部裁切" in steps[cut_idx])
check("工单声明中线仅为折叠线", "中线仅为折叠线" in steps[cut_idx])
# 2x2：纵切=单元列间槽0+外部1=1，横切=行间槽1 → 共 2 刀
check("2x2 裁切刀数=2（纵1+横1）", tk["plan"]["cuts_per_sheet"] == 2,
      str(tk["plan"]["cuts_per_sheet"]))
check("刀数=纵(单元列槽+1外部)+横(行槽)",
      tk["plan"]["cuts_per_sheet"] == tk["plan"]["cols"] // 2 + tk["plan"]["rows"] - 1)

# 工单与 PDF 标线一致：同一几何函数，坐标精确匹配
cells = tk["sheets"][0]["front"]["cells"]
geo = compute_mark_geometry(JobSpec(**tk["job"]), [CellModel(**c) for c in cells])
check("横向裁切线=行间废边边界[227,233]", geo["cut_h"] == [227.0, 233.0], str(geo["cut_h"]))
check("纵向外部裁切线 x=216", geo["ext_v"] == [216.0], str(geo["ext_v"]))
check("无单元间纵向裁切线", geo["cut_v"] == [])
check("折叠线=书脊中线 x=108", geo["fold_v"] == [108.0], str(geo["fold_v"]))
all_cuts = set(geo["cut_v"]) | set(geo["ext_v"]) | set(geo["cut_h"])
check("裁切线与折叠线无交集", not (set(geo["fold_v"]) & all_cuts))
# 裁切线（含外部线）不得穿过任何折页单元内部
units = {}
for c in cells:
    units.setdefault((c["row"], c["col"] // 2), []).append(c["x_mm"])
bad = []
for (row, cp), xs in units.items():
    lo, hi = min(xs), max(xs) + cells[0]["w_mm"]
    for x in geo["cut_v"] + geo["ext_v"]:
        if lo + 0.01 < x < hi - 0.01:
            bad.append((row, cp, x))
check("裁切线不穿过折页单元", not bad, str(bad))

# PDF 内容流：裁切虚线、折叠点划线、外部裁切实线（无线型设置）都在；图例文字在
r = post("/api/pdf", src, spec, {"rotation": 0, "cols": 2, "rows": 2})
check("2x2 pdf 200", r.status_code == 200)
out = PdfReader(io.BytesIO(r.content))
stream = out.pages[0].get_contents().get_data()
check("PDF 含裁切虚线线型", b"[4 3] 0 d" in stream)
check("PDF 含折叠点划线线型", b"[10 3] 0 d" in stream)
# 外部裁切实线：x=216mm=612.283pt 的竖线（mo 起点在页面底部 y=0）
check("PDF 含外部裁切实线坐标", b"612.28" in stream)
text = out.pages[0].extract_text()
check("PDF 图例表达先裁后折", "先外部裁切" in text and "中线禁裁" in text)

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

print("== 6. 胶装混合帖：50 页 / 允许 16、8 页帖 ==")
src50 = make_sample(50, out="/tmp/sample50.pdf")
spec_mix = {**SPEC_BASE, "binding": "perfect",
            "allowed_signature_pages": [16, 8], "max_signatures": 10}
r = post("/api/plans", src50, spec_mix)
check("混合帖 plans 200", r.status_code == 200, r.text[:300])
body = r.json()
check("混合帖有可行方案", len(body["plans"]) > 0)
ids = [p["id"] for p in body["plans"]]
check("候选 ID 唯一", len(set(ids)) == len(ids))
best_m = body["plans"][0]
print(f"  最优混合方案: {best_m['id']} 用纸{best_m['sheets_total']}张 "
      f"{best_m['signatures']}帖 空白{best_m['blank_pages']}页 "
      f"帖型{best_m['signature_types']}种 页数差{best_m['signature_spread']}")
# 50 页、8页/张容量：最优为 7 帖×8 页（容量 56、空白 6、单一帖型）
check("最优候选 ID", best_m["id"] == "rot0-2x2-mix8x7", best_m["id"])
check("最优用纸 7 张", best_m["sheets_total"] == 7)
check("容量 56 空白 6", best_m["capacity_pages"] == 56 and best_m["blank_pages"] == 6)
check("帖型 1 种页数差 0", best_m["signature_types"] == 1 and best_m["signature_spread"] == 0)
sp = best_m["signature_plan"]
check("候选含逐帖明细", sp is not None and len(sp) == best_m["signatures"])
check("逐帖页码范围连续覆盖", sp[0]["start_page"] == 1
      and all(sp[i]["end_page"] + 1 == sp[i + 1]["start_page"] for i in range(len(sp) - 1))
      and sp[-1]["end_page"] == best_m["capacity_pages"])
check("各帖页数为 4 的倍数且被单张容量整除",
      all(p["pages"] % 4 == 0 and p["pages"] % (best_m["units_per_sheet"] * 4) == 0 for p in sp))
check("逐帖用纸合计=总用纸", sum(p["sheets"] for p in sp) == best_m["sheets_total"])
check("空白页全部在末帖", best_m["blanks"] == sp[-1]["blanks"]
      and all(p["blanks"] == [] for p in sp[:-1]))
check("末帖至少 1 个真实页", sp[-1]["start_page"] <= 50)
check("帖码与配帖顺序一致",
      [p["mark"] for p in sp] == [f"帖{i + 1}/{len(sp)}" for i in range(len(sp))])
check("混合方案排序=总用纸→空白→帖型→页数差",
      body["plans"] == sorted(body["plans"], key=lambda p: (
          p["sheets_total"], p["blank_pages"], p["signature_types"], p["signature_spread"],
          p["signatures"], p["cuts_per_sheet"], -p["pages_per_side"])))

# 多帖型候选：16页×3帖＋8页×1帖
mix = next((p for p in body["plans"] if p["id"] == "rot0-2x2-mix16x3+8"), None)
check("多帖型候选存在", mix is not None)
parts = mix["signature_plan"]
check("逐帖页数 16/16/16/8", [p["pages"] for p in parts] == [16, 16, 16, 8])
check("逐帖用纸 2/2/2/1", [p["sheets"] for p in parts] == [2, 2, 2, 1])
check("末帖页码范围 49-56", parts[-1]["start_page"] == 49 and parts[-1]["end_page"] == 56)
check("末帖空白 51-56", parts[-1]["blanks"] == [51, 52, 53, 54, 55, 56])
check("前帖无空白", all(p["blanks"] == [] for p in parts[:-1]))

# 按候选 ID 出工单：页码网格按各帖容量生成，末帖空白不挤入前帖
r = post("/api/ticket", src50, spec_mix, {"plan_id": "rot0-2x2-mix16x3+8"})
check("按候选 ID 出工单", r.status_code == 200, r.text[:200])
tk = r.json()
check("工单方案=指定候选", tk["plan"]["id"] == "rot0-2x2-mix16x3+8")
sig_seq = [s["signature"] for s in tk["sheets"]]
check("工单配帖顺序 1,1,2,2,3,3,4", sig_seq == [1, 1, 2, 2, 3, 3, 4], str(sig_seq))
front_cells = [c for s in tk["sheets"] if s["signature"] < 4
               for c in s["front"]["cells"] + s["back"]["cells"]]
check("前帖页码网格无空白格", all(c["page"] is not None for c in front_cells))
last_cells = [c for s in tk["sheets"] if s["signature"] == 4
              for c in s["front"]["cells"] + s["back"]["cells"]]
sig4_pages = sorted(c["page"] for c in last_cells if c["page"] is not None)
check("末帖真实页仅 49、50", sig4_pages == [49, 50], str(sig4_pages))
check("末帖含 6 个空白格", sum(1 for c in last_cells if c["page"] is None) == 6)
steps_mix = tk["steps"]
gather = next(s for s in steps_mix if s.startswith("配帖"))
check("工单配帖步骤含构成与帖码顺序",
      "16页×3帖" in gather and "8页×1帖" in gather and "1→4" in gather, gather[:80])

# 按候选 ID 出 PDF：阶梯帖码/逐帖张数标注与候选一致
r = post("/api/pdf", src50, spec_mix, {"plan_id": "rot0-2x2-mix16x3+8"})
check("混合帖 pdf 200", r.status_code == 200, r.text[:200])
out = PdfReader(io.BytesIO(r.content))
check("PDF 页数=用纸×2", len(out.pages) == 14, str(len(out.pages)))
first_text = out.pages[0].extract_text()
check("首张标注 帖1/4·16页", "1/4" in first_text and "16页" in first_text, first_text[:60])
last_text = out.pages[-1].extract_text()
check("末张标注 帖4/4·8页", "4/4" in last_text and "8页" in last_text, last_text[:60])
check("末张张号分母=末帖张数 1/1", "1/1" in last_text)
open("/tmp/imposed_mix.pdf", "wb").write(r.content)

print("== 7. 混合帖无解：指出冲突约束 ==")
src100 = make_sample(100, out="/tmp/sample100.pdf")
# 帖数上限过小：100 页 > 3 帖 × 16 页 = 48 页容量
few = {**SPEC_BASE, "binding": "perfect",
       "allowed_signature_pages": [16], "max_signatures": 3}
r = post("/api/plans", src100, few)
body = r.json()
check("帖数不足无解", len(body["plans"]) == 0 and len(body["rejections"]) > 0)
rej_text = json.dumps(body["rejections"], ensure_ascii=False)
check("指出 max_signatures 冲突", "max_signatures" in rej_text and "最大帖数" in rej_text)
print(f"  原因示例: {body['rejections'][0]['reason']}")
r = post("/api/ticket", src100, few)
check("无解 ticket 返回 422", r.status_code == 422)

# 空白页上限过小：50 页只用 16 页帖 → 最小空白 14 > 4
tight = {**SPEC_BASE, "binding": "perfect",
         "allowed_signature_pages": [16], "max_blank_pages": 4}
r = post("/api/plans", src50, tight)
body = r.json()
rej_text = json.dumps(body["rejections"], ensure_ascii=False)
check("空白上限过小无解", len(body["plans"]) == 0 and len(body["rejections"]) > 0)
check("指出 max_blank_pages 冲突及最小空白 14",
      "max_blank_pages" in rej_text and "14" in rej_text)
print(f"  原因示例: {body['rejections'][0]['reason']}")

# 帖页数不被单张容量整除：12 页帖在 8 页/张开数不可行，在 4/12 页/张开数可行
# （短边纹使 rot90 开数可用，其中 2×3 开数单张容量恰为 12 页）
odd = {**SPEC_BASE, "binding": "perfect", "allowed_signature_pages": [12],
       "grain": "short_edge"}
r = post("/api/plans", src40, odd)
body = r.json()
check("12页帖有可行开数", len(body["plans"]) > 0)
check("最优为 12 页/张开数", body["plans"][0]["units_per_sheet"] * 4 == 12,
      f"units={body['plans'][0]['units_per_sheet']}")
check("最优方案容量 48 空白 8", body["plans"][0]["capacity_pages"] == 48
      and body["plans"][0]["blank_pages"] == 8)
rej_text = json.dumps(body["rejections"], ensure_ascii=False)
check("不整除的开数给出原因", "单张纸容量" in rej_text)
print(f"  原因示例: {body['rejections'][0]['reason']}")

print("== 8. 混合帖参数校验与兼容性 ==")
bad = {**SPEC_BASE, "binding": "perfect", "allowed_signature_pages": [10]}
r = post("/api/plans", src40, bad)
check("非4倍数帖页数返回 422", r.status_code == 422)
bad2 = {**SPEC_BASE, "binding": "perfect", "allowed_signature_pages": []}
r = post("/api/plans", src40, bad2)
check("空候选列表返回 422", r.status_code == 422)
# 骑马订忽略混合帖字段
saddle_mix = {**SPEC_BASE, "binding": "saddle",
              "allowed_signature_pages": [8, 16], "max_signatures": 2}
r = post("/api/plans", src, saddle_mix)
check("骑马订忽略混合帖字段", r.status_code == 200
      and r.json()["plans"][0]["sheets_total"] == 4)
# 未设置 allowed_signature_pages 时保持固定帖行为
r = post("/api/plans", src40, spec_p)
body = r.json()
check("固定帖行为保留", body["plans"][0]["capacity_pages"] % 16 == 0
      and body["plans"][0]["signature_plan"] is None)
# 现有开数选择器在混合帖模式仍可用
r = post("/api/ticket", src50, spec_mix, {"rotation": 0, "cols": 2, "rows": 2})
check("混合帖模式兼容开数选择器", r.status_code == 200
      and r.json()["plan"]["rotation"] == 0 and r.json()["plan"]["cols"] == 2)
r = post("/api/ticket", src50, spec_mix, {"plan_id": "no-such-plan"})
check("不存在的候选 ID 返回 404", r.status_code == 404)

print("== 9. 混合帖广候选集：组合器精确枚举不静默截断 ==")
from app.engine import _ComboSpaceTooLarge, _mixed_combos  # noqa: E402
import app.engine as eng  # noqa: E402

# 复现：400 页 + 4~200 全部 4 倍数帖型，旧实现漏掉排序更优的 (100,100,100,100)
wide_sizes = list(range(4, 201, 4))
combos = _mixed_combos(400, wide_sizes, 50, None, 10)
check("广候选集含 (100,100,100,100)", (100, 100, 100, 100) in combos)
check("最优组合为 (200,200)", combos[0] == (200, 200))
check("(200,100,100) 排不进前 10", (200, 100, 100) not in combos)
check("组合严格按 容量→帖型→页数差→帖数 排序",
      combos == sorted(combos, key=lambda c: (sum(c), len(set(c)), max(c) - min(c), len(c))))

# t≥3 精确补满：t1+t2 不足 50 个时由预算内 DFS 精确补足
combos50 = _mixed_combos(200, [16, 12, 8], 50, None, 50)
check("t≥3 精确补满 50 个候选且有序", len(combos50) == 50
      and combos50 == sorted(combos50, key=lambda c: (sum(c), len(set(c)), max(c) - min(c), len(c))))

# 超预算必须显式报错，不得静默截断返回不完整结果
old_budget = eng._COMBO_DFS_BUDGET
eng._COMBO_DFS_BUDGET = 10
try:
    _mixed_combos(200, [16, 12, 8], 50, None, 50)
    raised = False
except _ComboSpaceTooLarge:
    raised = True
finally:
    eng._COMBO_DFS_BUDGET = old_budget
check("超预算显式报错而非静默截断", raised)

# API 级回归：400 页广候选集，最优候选与排序完整性
src400 = make_sample(400, out="/tmp/sample400.pdf")
wide = {**SPEC_BASE, "binding": "perfect",
        "allowed_signature_pages": wide_sizes, "max_layouts": 8}
r = post("/api/plans", src400, wide)
check("广候选集 plans 200", r.status_code == 200, r.text[:200])
body = r.json()
ids = [p["id"] for p in body["plans"]]
check("最优候选 (200,200)", ids[0] == "rot0-2x2-mix200x2", ids[0])
check("全部候选零空白（容量恰为 400）", all(p["blank_pages"] == 0 for p in body["plans"]))
check("候选严格按排序键有序", body["plans"] == sorted(body["plans"], key=lambda p: (
    p["sheets_total"], p["blank_pages"], p["signature_types"], p["signature_spread"],
    p["signatures"], p["cuts_per_sheet"], -p["pages_per_side"])))

# 仅 2x1 开数可行（单张容量 4 页）：(100,100,100,100) 必须进入候选
narrow = {**SPEC_BASE, "binding": "perfect", "grain": "short_edge",
          "sheet_width": 230, "sheet_height": 170,
          "allowed_signature_pages": wide_sizes, "max_layouts": 8}
r = post("/api/plans", src400, narrow)
body = r.json()
ids = [p["id"] for p in body["plans"]]
check("(100x4) 进入候选且仅次于 (200x2)",
      ids[0] == "rot0-2x1-mix200x2" and ids[1] == "rot0-2x1-mix100x4", str(ids[:3]))

print("== 10. 骑马订爬移补偿 /api/creep ==")
CREEP = {"paper_thickness": 0.1, "compression_factor": 0.8,
         "binding_width": 6, "max_offset": 0.5}
r = post("/api/creep", src, spec, CREEP)
check("creep 200", r.status_code == 200, r.text[:300])
cr = r.json()
check("方案=最优骑马订候选", cr["plan_id"] == best["id"])
check("步距=厚度×系数 0.08", cr["step_mm"] == 0.08, str(cr["step_mm"]))
check("用纸张数一致", cr["sheets_total"] == best["sheets_total"])
check("套帖层位 1→N", [s["nesting"] for s in cr["sheets"]] == [1, 2, 3, 4])
check("逐张补偿 0/0.08/0.16/0.24",
      [s["creep_mm"] for s in cr["sheets"]] == [0.0, 0.08, 0.16, 0.24],
      str([s["creep_mm"] for s in cr["sheets"]]))
s1, s4 = cr["sheets"][0], cr["sheets"][3]
left_p = [p for p in s1["pages"] if p["side"] == "F" and p["col"] == 0][0]
right_p = [p for p in s4["pages"] if p["side"] == "F" and p["col"] == 1][0]
left4 = [p for p in s4["pages"] if p["side"] == "F" and p["col"] == 0][0]
check("左页向 +x 补偿", left4["compensated"][0] == round(left4["original"][0] + 0.24, 3)
      and left4["shift_mm"] == 0.24, str(left4["compensated"]))
check("右页向 -x 补偿", right_p["compensated"][0] == round(right_p["original"][0] - 0.24, 3)
      and right_p["shift_mm"] == -0.24, str(right_p["compensated"]))
check("y 坐标不补偿", all(p["compensated"][1] == p["original"][1]
                          for s in cr["sheets"] for p in s["pages"]))
check("最外层补偿为 0", all(p["shift_mm"] == 0 for p in s1["pages"]))
check("书脊中线不随补偿移动", left_p["spine_x_mm"] == left4["spine_x_mm"] == 108.0,
      str(left_p["spine_x_mm"]))
# 每张纸正反面共 pages_per_side×2 格；32 页 4 张共 32 个真实页格（每页印刷一次）
check("页码逐页保留（含正反面）",
      sorted(p["page"] for s in cr["sheets"] for p in s["pages"] if p["page"])
      == list(range(1, 33)))
check("每页恰好一个页格（正/反面合计）",
      sum(1 for s in cr["sheets"] for p in s["pages"] if p["page"]) == 32)
check("装订区以中线为中心", cr["binding_zone"][0]["center_x"] == 108.0
      and cr["binding_zone"][0]["left"] == 105.0 and cr["binding_zone"][0]["right"] == 111.0)
check("默认参数无诊断", cr["error_count"] == 0 and cr["warning_count"] == 0
      and cr["diagnostics"] == [])
check("含书口方向与公式说明", "书口" in cr["fore_edge_direction"] and "step" in cr["formula"])

# 超限：0.5mm/张（×系数0.8 → 步距0.4）→ 第2张起 0.4/0.8/1.2 均 > max_offset=0.5
r = post("/api/creep", src, spec, {**CREEP, "paper_thickness": 0.5})
cr_over = r.json()
check("补偿超限仍返回 200", r.status_code == 200)
check("超限诊断 offset_exceeded",
      any(d["code"] == "offset_exceeded" for d in cr_over["diagnostics"]))
check("超限纸张逐张报告（步距0.4，第3/4张 0.8/1.2>0.5）",
      sorted({d["sheet"] for d in cr_over["diagnostics"] if d["code"] == "offset_exceeded"})
      == [3, 4])
inner_pages = [p for p in cr_over["sheets"][3]["pages"] if p["page"] is not None]
check("超限标注落到逐页诊断码", all("offset_exceeded" in p["diagnostics"] for p in inner_pages))
check("第1张不超限", all("offset_exceeded" not in p["diagnostics"]
                         for p in cr_over["sheets"][0]["pages"]))

# 装订区越界：宽度超过纸张/相邻单元重叠
r = post("/api/creep", src, spec, {**CREEP, "binding_width": 300})
check("装订区越界诊断", r.status_code == 200
      and any(d["code"] == "binding_out_of_bounds" for d in r.json()["diagnostics"]))
big = {"finished_width": 105, "finished_height": 148, "sheet_width": 460, "sheet_height": 470,
       "grain": "long_edge", "bleed": 3, "gripper": 10, "gripper_edge": "bottom",
       "flip": "long_edge", "binding": "saddle"}
r = post("/api/creep", src, big, {**CREEP, "plan_id": "rot0-4x2", "binding_width": 230})
codes = {d["code"] for d in r.json()["diagnostics"]}
check("多列相邻单元装订区重叠诊断", "binding_out_of_bounds" in codes, str(codes))

# 正反面套准：正常方案 0 错位
check("正反面套准无错位", not any(d["code"] == "register_mismatch" for d in cr["diagnostics"]))

# 短边翻转：套准校核同样通过
spec_se = {**spec, "flip": "short_edge", "grain": "short_edge"}
r = post("/api/creep", src, spec_se, CREEP)
check("短边翻转 creep 200 且无错位/越界", r.status_code == 200
      and not any(d["code"] in ("register_mismatch", "out_of_printable")
                  for d in r.json()["diagnostics"]), r.text[:200])
# 短边翻转的背对背页：正面 (2,0)=32 绕短边翻转后对应背面 (0,0)=31，位移一致
se = r.json()
f00 = next(p for p in se["sheets"][0]["pages"] if p["side"] == "F" and p["row"] == 2 and p["col"] == 0)
b10 = next(p for p in se["sheets"][0]["pages"] if p["side"] == "B" and p["row"] == 0 and p["col"] == 0)
check("短边翻转背页配对(32↔31)", f00["page"] == 32 and b10["page"] == 31,
      f"{f00['page']}↔{b10['page']}")
check("短边翻转动纸张位移一致", abs(f00["shift_mm"]) == abs(b10["shift_mm"]))

# 页面越出可印区域：左侧咬口 + 厚纸多帖，内层右页 -x 后带出血矩形进入咬口
src96 = make_sample(96, out="/tmp/sample96.pdf")
left_grip = {**spec, "gripper_edge": "left"}  # 长边纹正放，选 2×1 开数（24 张）
r = post("/api/plans", src96, left_grip)
pid_left = "rot0-2x1"
r = post("/api/creep", src96, left_grip,
         {"plan_id": pid_left, "paper_thickness": 5.0, "binding_width": 6, "max_offset": 500})
check("左咬口厚纸大补偿 200", r.status_code == 200)
oop = [d for d in r.json()["diagnostics"] if d["code"] == "out_of_printable"]
check("页面越出可印区域诊断", bool(oop), str([d["code"] for d in r.json()["diagnostics"]][:8]))
check("越界诊断指明可印区域", oop and "可印区域" in oop[0]["message"])
check("越界诊断定位到纸张/正反面/页码",
      all(d["sheet"] is not None and d["side"] and d["page"] is not None for d in oop))

# 参数与装订方式校验
r = post("/api/creep", src40, spec_p, CREEP)
check("胶装拒绝爬移分析", r.status_code == 422)
r = post("/api/creep", src, spec, {k: v for k, v in CREEP.items() if k != "binding_width"})
check("缺装订区宽度返回 422", r.status_code == 422)
r = post("/api/creep", src, spec, {**CREEP, "compression_factor": 0})
check("压缩系数为 0 返回 422", r.status_code == 422)
r = post("/api/creep", src, spec, {**CREEP, "plan_id": "no-such-plan"})
check("不存在候选 ID 返回 404", r.status_code == 404)

print("== 11. /api/ticket 与 /api/pdf 共用爬移参数 ==")
r = post("/api/ticket", src, spec, CREEP)
check("工单含 creep 段", r.status_code == 200 and r.json()["creep"] is not None)
tk = r.json()
check("工单 creep 与 /api/creep 同参（步距/逐张）",
      tk["creep"]["step_mm"] == 0.08
      and [s["creep_mm"] for s in tk["creep"]["sheets"]] == [0.0, 0.08, 0.16, 0.24])
check("工单原 sheets 坐标不被修改",
      all("creep" not in c for c in tk["sheets"][1]["front"]["cells"])
      and tk["sheets"][1]["front"]["cells"][0]["x_mm"]
      == best["sheets"][1]["front"]["cells"][0]["x_mm"])
check("工单 creep 逐页含原/补偿坐标与诊断",
      tk["creep"]["sheets"][1]["pages"][0]["original"] is not None
      and "diagnostics" in tk["creep"]["sheets"][1]["pages"][0])

r0 = post("/api/pdf", src, spec)
r1 = post("/api/pdf", src, spec, CREEP)
check("爬移 PDF 200", r1.status_code == 200, r1.text[:200])
check("无爬移时 PDF 与基线一致", r0.content == post("/api/pdf", src, spec).content)
out0 = PdfReader(io.BytesIO(r0.content))
out1 = PdfReader(io.BytesIO(r1.content))
s0 = out0.pages[2].get_contents().get_data()
s1s = out1.pages[2].get_contents().get_data()
# 第2张正面首格：trim 左缘 3.0→3.08mm；rotation=0 时合并页矩阵平移 e 直接在流中
# 基线 e = 3mm = 8.50393701pt；补偿后 e = 3.08mm = 8.73070866pt（pypdf 保留 8 位小数）
check("PDF 基线置入矩阵 x=8.50393701pt", b"8.50393701" in s0)
check("PDF 按补偿坐标置入（x=8.73070866pt）",
      b"8.73070866" in s1s and b"8.50393701" not in s1s)
text = out1.pages[2].extract_text()
check("PDF 标出爬移量", "爬移补偿 0.080mm" in text and "爬移 0.080mm" in text, text[:80])
check("PDF 标出装订区与书口图例", "装订区边界" in text and "书口" in text)
# 装订区边界 105/111mm 与书脊中线 108mm 同时在（坐标以 8 位小数写出）
check("装订区边界线在 PDF 中",
      f"{105 * 72 / 25.4:.4f}".encode() in s1s and f"{111 * 72 / 25.4:.4f}".encode() in s1s)
check("书脊中线仍在原位（不随补偿移动）", f"{108 * 72 / 25.4:.4f}".encode() in s1s)
check("下载文件名含爬移标识", "creep" in r1.headers["content-disposition"])
open("/tmp/imposed_creep.pdf", "wb").write(r1.content)

# 原候选不受预览/导出影响：重新枚举坐标与基线一致
r2 = post("/api/plans", src, spec)
check("预览/导出后原候选不变",
      r2.json()["plans"][0]["sheets"][1]["front"]["cells"][0]["x_mm"]
      == best["sheets"][1]["front"]["cells"][0]["x_mm"])

# 未提供纸张厚度：保持现有拼版结果（PDF 字节一致）
check("未提供厚度 PDF 字节一致", post("/api/pdf", src, spec).content == r0.content)
tk_none = post("/api/ticket", src, spec).json()
check("未提供厚度工单 creep=null", tk_none["creep"] is None)

# 胶装提供爬移参数：忽略且 PDF 不变
rp0 = post("/api/pdf", src40, spec_p).content
rpc = post("/api/pdf", src40, spec_p, CREEP).content
check("胶装 PDF 不受爬移参数影响", rp0 == rpc)
tpc = post("/api/ticket", src40, spec_p, CREEP).json()
check("胶装工单 creep=null", tpc["creep"] is None)

# 只给部分爬移参数 → 422
r = post("/api/ticket", src, spec, {"paper_thickness": 0.1})
check("仅给厚度缺装订区参数返回 422", r.status_code == 422)

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过 ✔  样例输出: /tmp/imposed_saddle.pdf, /tmp/imposed_perfect.pdf, "
      "/tmp/imposed_mix.pdf, /tmp/imposed_creep.pdf")
