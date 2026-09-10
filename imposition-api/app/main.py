"""印前拼版 REST API：骑马订 / 胶装。全部运算在本地完成。"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pypdf import PdfReader

from .engine import enumerate_plans, find_plan
from .pdf_builder import build_imposed_pdf
from .schemas import JobSpec, LayoutSelector, PlanModel, PlansResponse

app = FastAPI(
    title="印前拼版 API",
    description="小型印刷厂骑马订/胶装拼版方案生成：枚举开数与横竖放置、校验尺寸/咬口/纹向、"
                "胶装支持固定帖与混合帖规划（多种帖页数组合配帖），"
                "输出带裁切线/套准标记/帖码的拼版 PDF 与 JSON 工单。所有处理均在本地完成。",
    version="1.1.0",
)

PT_PER_MM = 72.0 / 25.4
SIZE_TOL_MM = 1.5  # 页面尺寸容差


def _validate_source(pdf_bytes: bytes, spec: JobSpec) -> dict:
    """校验源 PDF：页数、页面尺寸一致且等于成品尺寸（或含出血尺寸）。"""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise HTTPException(422, f"无法解析 PDF 文件：{exc}") from exc
    n = len(reader.pages)
    if n == 0:
        raise HTTPException(422, "源 PDF 没有页面")

    sizes = set()
    for p in reader.pages:
        w = float(p.mediabox.width) / PT_PER_MM
        h = float(p.mediabox.height) / PT_PER_MM
        sizes.add((round(w, 1), round(h, 1)))
    if len(sizes) != 1:
        raise HTTPException(422, f"源 PDF 页面尺寸不一致：{sorted(sizes)}，请统一为成品尺寸")
    (w, h), = sizes

    fw, fh, b = spec.finished_width, spec.finished_height, spec.bleed
    exact = abs(w - fw) <= SIZE_TOL_MM and abs(h - fh) <= SIZE_TOL_MM
    with_bleed = abs(w - (fw + 2 * b)) <= SIZE_TOL_MM and abs(h - (fh + 2 * b)) <= SIZE_TOL_MM
    if not (exact or with_bleed):
        raise HTTPException(
            422,
            f"源页面尺寸 {w}×{h}mm 与成品尺寸 {fw}×{fh}mm 不符"
            f"（允许成品尺寸或含出血 {fw + 2 * b}×{fh + 2 * b}mm，容差 {SIZE_TOL_MM}mm）",
        )
    return {
        "pages": n,
        "page_width_mm": w,
        "page_height_mm": h,
        "has_bleed": with_bleed and not exact,
    }


def _parse_spec(spec_json: str) -> JobSpec:
    try:
        return JobSpec.model_validate(json.loads(spec_json))
    except Exception as exc:
        raise HTTPException(422, f"工单参数无效：{exc}") from exc


def _pick_plan(plans: list[PlanModel], sel: LayoutSelector) -> PlanModel:
    plan = find_plan(plans, sel.rotation, sel.cols, sel.rows, sel.plan_id)
    if plan is None:
        if sel.plan_id:
            raise HTTPException(
                404,
                f"未找到候选 ID 为 “{sel.plan_id}” 的方案，请先调用 /api/plans 查看候选 ID",
            )
        raise HTTPException(
            404,
            f"未找到匹配的方案 rotation={sel.rotation} cols={sel.cols} rows={sel.rows}，"
            "请先调用 /api/plans 查看可行方案",
        )
    return plan


def _ticket(spec: JobSpec, source: dict, plan: PlanModel) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job": spec.model_dump(),
        "source": source,
        "plan": plan.model_dump(exclude={"sheets"}),
        "sheets": [s.model_dump() for s in plan.sheets],
        "steps": plan.steps,
        "notes": [
            "页码为 null 的单元格为空白页",
            "rotation 为该页内容置入时的旋转角度（度）",
            "坐标原点在纸张左下角，单位 mm",
            "混合帖方案见 plan.signature_plan：逐帖页码范围/容量/用纸/空白页位置/帖码，"
            "配帖顺序与 PDF 阶梯帖码均按帖序号 1→N",
        ],
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/plans", response_model=PlansResponse)
async def list_plans(
    file: UploadFile = File(..., description="源 PDF"),
    spec: str = Form(..., description="JobSpec JSON 字符串"),
) -> PlansResponse:
    """枚举可行的开数与横竖放置方案，按纸张用量→空白页→裁切次数排序。"""
    job = _parse_spec(spec)
    pdf_bytes = await file.read()
    source = _validate_source(pdf_bytes, job)
    plans, rejections = enumerate_plans(job, source["pages"])
    return PlansResponse(source=source, spec=job, plans=plans, rejections=rejections)


@app.post("/api/ticket")
async def make_ticket(
    file: UploadFile = File(..., description="源 PDF"),
    spec: str = Form(...),
    plan_id: str | None = Form(default=None, description="候选方案 ID（优先于开数选择器）"),
    rotation: int | None = Form(default=None),
    cols: int | None = Form(default=None),
    rows: int | None = Form(default=None),
) -> dict:
    """生成供现场核对的 JSON 工单（默认取排序最优方案）。"""
    job = _parse_spec(spec)
    pdf_bytes = await file.read()
    source = _validate_source(pdf_bytes, job)
    plans, rejections = enumerate_plans(job, source["pages"])
    if not plans:
        raise HTTPException(422, {"message": "无可行方案", "reasons": [r.model_dump() for r in rejections]})
    plan = _pick_plan(plans, LayoutSelector(plan_id=plan_id, rotation=rotation, cols=cols, rows=rows))
    return _ticket(job, source, plan)


@app.post("/api/pdf")
async def make_pdf(
    file: UploadFile = File(..., description="源 PDF"),
    spec: str = Form(...),
    plan_id: str | None = Form(default=None, description="候选方案 ID（优先于开数选择器）"),
    rotation: int | None = Form(default=None),
    cols: int | None = Form(default=None),
    rows: int | None = Form(default=None),
) -> Response:
    """输出拼版 PDF：含裁切线、套准标记、帖码，正反面交替分页。"""
    job = _parse_spec(spec)
    pdf_bytes = await file.read()
    source = _validate_source(pdf_bytes, job)
    plans, rejections = enumerate_plans(job, source["pages"])
    if not plans:
        raise HTTPException(422, {"message": "无可行方案", "reasons": [r.model_dump() for r in rejections]})
    plan = _pick_plan(plans, LayoutSelector(plan_id=plan_id, rotation=rotation, cols=cols, rows=rows))
    out = build_imposed_pdf(pdf_bytes, job, plan, source["has_bleed"])
    return Response(
        content=out,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="imposed_{plan.id}.pdf"'},
    )
