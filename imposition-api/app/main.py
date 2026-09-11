"""印前拼版 REST API：骑马订 / 胶装。全部运算在本地完成。"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pypdf import PdfReader

from .creep import compute_creep
from .engine import enumerate_plans, find_plan
from .pdf_builder import build_imposed_pdf
from .schemas import (
    CreepParams,
    CreepResponse,
    JobSpec,
    LayoutSelector,
    PlanModel,
    PlansResponse,
)

app = FastAPI(
    title="印前拼版 API",
    description="小型印刷厂骑马订/胶装拼版方案生成：枚举开数与横竖放置、校验尺寸/咬口/纹向、"
                "胶装支持固定帖与混合帖规划（多种帖页数组合配帖），"
                "骑马订支持爬移补偿（creep）与装订区校核，"
                "输出带裁切线/套准标记/帖码/装订区与爬移标注的拼版 PDF 与 JSON 工单。"
                "所有处理均在本地完成。",
    version="1.2.0",
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


def _parse_creep_params(
    paper_thickness: float | None,
    compression_factor: float | None,
    binding_width: float | None,
    max_offset: float | None,
) -> CreepParams | None:
    """解析爬移补偿表单参数；未提供纸张厚度时返回 None（保持原拼版结果）。

    一旦提供纸张厚度，装订区宽度与最大允许偏移必须同时提供；压缩系数缺省 1.0。
    """
    if paper_thickness is None:
        if any(v is not None for v in (compression_factor, binding_width, max_offset)):
            raise HTTPException(422, "爬移补偿需提供 paper_thickness；"
                                     "仅给出压缩系数/装订区宽度/最大偏移不生效")
        return None
    raw = {
        "paper_thickness": paper_thickness,
        "compression_factor": compression_factor if compression_factor is not None else 1.0,
        "binding_width": binding_width,
        "max_offset": max_offset,
    }
    if binding_width is None or max_offset is None:
        raise HTTPException(422, "爬移补偿需同时提供 paper_thickness、binding_width、max_offset"
                                 "（compression_factor 缺省为 1.0）")
    try:
        return CreepParams.model_validate(raw)
    except Exception as exc:
        raise HTTPException(422, f"爬移补偿参数无效：{exc}") from exc


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


def _creep_for(job: JobSpec, plan: PlanModel, params: CreepParams | None,
               source: dict | None = None):
    """骑马订且提供补偿参数时计算爬移报告；其余情形（胶装/未提供厚度）返回 None。"""
    if params is None:
        return None
    if job.binding.value != "saddle":
        # 胶装按原逻辑处理：不计算爬移，参数被显式忽略
        return None
    report = compute_creep(job, plan, params)
    if source is not None:
        report.source = source
    return report


def _ticket(spec: JobSpec, source: dict, plan: PlanModel, creep=None) -> dict:
    ticket = {
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
    if creep is not None:
        ticket["creep"] = creep.model_dump()
        ticket["notes"].append(
            "creep 为骑马订爬移补偿结果：纸张由外到内 1→N 套叠，最外层补偿 0；"
            "sheets[].pages[] 给出逐页 original/compensated 坐标与 shift，"
            "diagnostics 为逐项校核（offset_exceeded/out_of_printable/"
            "register_mismatch/binding_out_of_bounds）；sheets 中的坐标仍为补偿前原候选，"
            "导出 PDF 按 creep 补偿后坐标置入，原候选不被修改")
    else:
        ticket["creep"] = None
        if spec.binding.value == "perfect":
            ticket["notes"].append("胶装不做爬移补偿；若提供爬移参数将被忽略")
    return ticket


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


@app.post("/api/creep", response_model=CreepResponse)
async def creep_analysis(
    file: UploadFile = File(..., description="源 PDF"),
    spec: str = Form(..., description="JobSpec JSON 字符串（须为骑马订 saddle）"),
    plan_id: str | None = Form(default=None, description="骑马订候选方案 ID（优先于开数选择器）"),
    rotation: int | None = Form(default=None),
    cols: int | None = Form(default=None),
    rows: int | None = Form(default=None),
    paper_thickness: float = Form(..., description="单张纸厚度 mm"),
    compression_factor: float = Form(default=1.0, description="压缩系数（步距=厚度×系数）"),
    binding_width: float = Form(..., description="装订区宽度 mm（以书脊中线为中心）"),
    max_offset: float = Form(..., description="最大允许补偿偏移 mm"),
) -> CreepResponse:
    """骑马订爬移补偿与装订区校核：逐张（由外到内）/正反面/逐页给出原坐标、
    补偿后坐标、页码与逐项诊断（补偿超限、越出可印区域、正反面套准错位、装订区标记越界）。"""
    job = _parse_spec(spec)
    if job.binding.value != "saddle":
        raise HTTPException(422, "爬移补偿仅适用于骑马订（binding=saddle）；胶装按原逻辑处理")
    pdf_bytes = await file.read()
    source = _validate_source(pdf_bytes, job)
    plans, rejections = enumerate_plans(job, source["pages"])
    if not plans:
        raise HTTPException(422, {"message": "无可行方案", "reasons": [r.model_dump() for r in rejections]})
    plan = _pick_plan(plans, LayoutSelector(plan_id=plan_id, rotation=rotation, cols=cols, rows=rows))
    params = _parse_creep_params(paper_thickness, compression_factor, binding_width, max_offset)
    report = compute_creep(job, plan, params)
    report.source = source
    return report


@app.post("/api/ticket")
async def make_ticket(
    file: UploadFile = File(..., description="源 PDF"),
    spec: str = Form(...),
    plan_id: str | None = Form(default=None, description="候选方案 ID（优先于开数选择器）"),
    rotation: int | None = Form(default=None),
    cols: int | None = Form(default=None),
    rows: int | None = Form(default=None),
    paper_thickness: float | None = Form(default=None, description="爬移：单张纸厚度 mm（骑马订）"),
    compression_factor: float | None = Form(default=None, description="爬移：压缩系数"),
    binding_width: float | None = Form(default=None, description="爬移：装订区宽度 mm"),
    max_offset: float | None = Form(default=None, description="爬移：最大允许补偿偏移 mm"),
) -> dict:
    """生成供现场核对的 JSON 工单（默认取排序最优方案）。

    骑马订提供 paper_thickness/binding_width/max_offset 时，工单追加 creep 段：
    逐张补偿量、逐页原/补偿后坐标与逐项诊断；参数与 /api/pdf 完全一致。
    """
    job = _parse_spec(spec)
    pdf_bytes = await file.read()
    source = _validate_source(pdf_bytes, job)
    plans, rejections = enumerate_plans(job, source["pages"])
    if not plans:
        raise HTTPException(422, {"message": "无可行方案", "reasons": [r.model_dump() for r in rejections]})
    plan = _pick_plan(plans, LayoutSelector(plan_id=plan_id, rotation=rotation, cols=cols, rows=rows))
    creep_params = _parse_creep_params(paper_thickness, compression_factor,
                                       binding_width, max_offset)
    creep = _creep_for(job, plan, creep_params, source)
    return _ticket(job, source, plan, creep)


@app.post("/api/pdf")
async def make_pdf(
    file: UploadFile = File(..., description="源 PDF"),
    spec: str = Form(...),
    plan_id: str | None = Form(default=None, description="候选方案 ID（优先于开数选择器）"),
    rotation: int | None = Form(default=None),
    cols: int | None = Form(default=None),
    rows: int | None = Form(default=None),
    paper_thickness: float | None = Form(default=None, description="爬移：单张纸厚度 mm（骑马订）"),
    compression_factor: float | None = Form(default=None, description="爬移：压缩系数"),
    binding_width: float | None = Form(default=None, description="爬移：装订区宽度 mm"),
    max_offset: float | None = Form(default=None, description="爬移：最大允许补偿偏移 mm"),
) -> Response:
    """输出拼版 PDF：含裁切线、套准标记、帖码，正反面交替分页。

    骑马订提供爬移参数时按补偿后坐标置入页面，并标出书口方向（橙箭头）、
    装订区边界（蓝点线）与逐张偏移量；不修改原候选方案。
    """
    job = _parse_spec(spec)
    pdf_bytes = await file.read()
    source = _validate_source(pdf_bytes, job)
    plans, rejections = enumerate_plans(job, source["pages"])
    if not plans:
        raise HTTPException(422, {"message": "无可行方案", "reasons": [r.model_dump() for r in rejections]})
    plan = _pick_plan(plans, LayoutSelector(plan_id=plan_id, rotation=rotation, cols=cols, rows=rows))
    creep_params = _parse_creep_params(paper_thickness, compression_factor,
                                       binding_width, max_offset)
    creep = _creep_for(job, plan, creep_params, source)
    out = build_imposed_pdf(pdf_bytes, job, plan, source["has_bleed"], creep_report=creep)
    suffix = f"_creep_t{creep.params.paper_thickness:g}" if creep is not None else ""
    return Response(
        content=out,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="imposed_{plan.id}{suffix}.pdf"'},
    )
