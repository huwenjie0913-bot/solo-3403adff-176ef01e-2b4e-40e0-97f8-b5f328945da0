"""骑马订爬移（creep）补偿与装订区校核。

套帖模型
--------
骑马订全册为一帖：纸张按"由外到内"顺序 1→N 套叠在书脊上。每张纸自身有厚度，
折叠后内层纸沿书脊绕行的路径更长，因此内层页书口（与书脊相对的自由边）天然向外
探出；三面切书按最外层页切齐后，内层页的成书白边（书口侧）偏窄、内容相对外移。

补偿方向与现有拼版约定一致：单元左页（col 偶）向 +x 移、右页（col 奇）向 -x 移
（二者均指向该单元书脊中线 fold_v），即把内容整体向书脊中线挪，预留下方裁切余量。

补偿量
------
- 最外层纸（nesting=1）补偿 0；
- 每向内一张纸（nesting 加 1），多补偿一个步距 step = 纸张厚度 × 压缩系数；
- 第 i 张纸（由外到内 1 起）补偿量 creep_i = step × (i - 1)；
  最内层纸张补偿最大。本 API 不自动缩放，若 creep_i 超过 max_offset 则逐项诊断为
  补偿超限（offset_exceeded），由现场决定调纸/调工艺或接受。

所有长度单位 mm；坐标原点为纸张左下角，x 向右（书脊法向），y 向上。
诊断不阻断返回：report 始终给出逐页原坐标/补偿后坐标与逐项诊断。
"""
from __future__ import annotations

from .engine import printable_area
from .schemas import (
    CellModel,
    CreepDiagnostic,
    CreepPageModel,
    CreepParams,
    CreepResponse,
    CreepSheetModel,
    FlipMode,
    JobSpec,
    PlanModel,
)

TOL_MM = 0.01  # 坐标校核容差（mm）


def _diag(code: str, message: str, severity: str = "error",
          sheet: int | None = None, side: str | None = None,
          page: int | None = None) -> CreepDiagnostic:
    return CreepDiagnostic(code=code, severity=severity, sheet=sheet,
                           side=side, page=page, message=message)


def _spine_x(cells: list[CellModel], cell: CellModel) -> float:
    """该格所靠书脊中线 x：偶数列（单元左格）的中线 = 自身 trim 右缘。"""
    cw = cells[0].w_mm
    if cell.col % 2 == 0:
        return round(cell.x_mm + cw, 3)
    return round(cell.x_mm, 3)


def compute_creep(spec: JobSpec, plan: PlanModel, params: CreepParams) -> CreepResponse:
    """按由外到内套帖顺序计算逐张/逐面/逐页补偿量，并完成四项校核。

    校核：
    1. offset_exceeded      单张补偿量超过 max_offset；
    2. out_of_printable     补偿后页面（含出血矩形）越出扣除咬口的可印区域或纸张；
    3. register_mismatch    同一张纸正反面背对背页面套准错位（翻转镜像后不重合）；
    4. binding_out_of_bounds 装订区标记越界（装订区出纸、入咬口、伸入相邻单元或
                             伸出折页线外）。
    """
    if spec.binding.value != "saddle":
        raise ValueError("爬移补偿仅适用于骑马订（binding=saddle）")

    step = round(params.paper_thickness * params.compression_factor, 6)
    px0, py0, pw, ph = printable_area(spec)
    px1, py1 = px0 + pw, py0 + ph
    sw, sh = spec.sheet_width, spec.sheet_height
    bleed = spec.bleed
    half = params.binding_width / 2.0
    cw = plan.sheets[0].front.cells[0].w_mm
    ch = plan.sheets[0].front.cells[0].h_mm

    # 正反面互为背对背的镜像常数：长边翻转绕水平轴（x 镜像），短边翻转绕竖直轴（y 镜像）
    f_cells0 = plan.sheets[0].front.cells
    x_min = min(c.x_mm for c in f_cells0)
    x_max = max(c.x_mm for c in f_cells0)
    y_min = min(c.y_mm for c in f_cells0)
    y_max = max(c.y_mm for c in f_cells0)
    kx = round(x_min + x_max + cw, 3)
    ky = round(y_min + y_max + ch, 3)

    # 折页单元装订区（以正面首格网格为准，正反面折叠线一致）：逐单元一条
    fold_cells = {(c.col // 2, c.row): c for c in f_cells0 if c.col % 2 == 0}
    binding_zone: list[dict] = []
    spine_by_unit: dict[tuple[int, int], float] = {}
    for (cp, row), cell in sorted(fold_cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        sx = round(cell.x_mm + cw, 3)
        unit = row * (plan.cols // 2) + cp + 1
        spine_by_unit[(cp, row)] = sx
        binding_zone.append({
            "unit": unit, "row": row, "center_x": sx,
            "left": round(sx - half, 3), "right": round(sx + half, 3),
        })

    # —— 装订区标记校核（与具体纸张无关，按折页单元做一次）——
    global_diags: list[CreepDiagnostic] = []
    for z in binding_zone:
        u, left, right = z["unit"], z["left"], z["right"]
        if left < -TOL_MM or right > sw + TOL_MM:
            global_diags.append(_diag(
                "binding_out_of_bounds",
                f"单元{u}装订区 [{left:.3f}, {right:.3f}]mm 越出纸张 0~{sw:g}mm",
            ))
        if left < px0 - TOL_MM or right > px1 + TOL_MM:
            global_diags.append(_diag(
                "binding_out_of_bounds",
                f"单元{u}装订区 [{left:.3f}, {right:.3f}]mm 伸入咬口/越出可印区域 "
                f"[{px0:g}, {px1:g}]mm",
            ))
    # 相邻单元装订区重叠（骑订钉位互相冲突）
    for z1, z2 in zip(binding_zone, binding_zone[1:]):
        if z1["row"] == z2["row"] and z1["right"] > z2["left"] + TOL_MM:
            global_diags.append(_diag(
                "binding_out_of_bounds",
                f"第{z1['row']+1}行相邻单元{z1['unit']}/{z2['unit']}装订区重叠："
                f"{z1['unit']} 右界 {z1['right']:.3f}mm > {z2['unit']} 左界 {z2['left']:.3f}mm",
            ))
    # 装订区伸出折页线外（折页线本身位于装订区中心，装订区总宽超过单元两页可容纳宽度；
    # 以中线两侧各 half 不超过"中线到外侧裁切线"余量校核）
    for (cp, row), sx in spine_by_unit.items():
        left_cell = next(c for c in f_cells0 if c.col // 2 == cp and c.row == row and c.col % 2 == 0)
        right_cell = next(c for c in f_cells0 if c.col // 2 == cp and c.row == row and c.col % 2 == 1)
        room_l = sx - (left_cell.x_mm - bleed)   # 中线到左页出血外缘
        room_r = (right_cell.x_mm + cw + bleed) - sx
        if half > room_l + TOL_MM or half > room_r + TOL_MM:
            unit = row * (plan.cols // 2) + cp + 1
            global_diags.append(_diag(
                "binding_out_of_bounds",
                f"单元{unit}装订区半宽 {half:g}mm 伸出折页线两侧出血外缘"
                f"（两侧余量 {min(room_l, room_r):.3f}mm）",
            ))

    all_diags: list[CreepDiagnostic] = list(global_diags)
    sheet_models: list[CreepSheetModel] = []

    for sheet in plan.sheets:
        i = sheet.index  # 骑马订单帖：纸张序号即由外到内层位（1 = 最外层）
        creep = round(step * (i - 1), 3)
        sheet_diags: list[CreepDiagnostic] = []
        page_records: list[CreepPageModel] = []

        # 1) 逐页补偿与"超限/越出可印区域"诊断
        for side_name, side in (("F", sheet.front), ("B", sheet.back)):
            for cell in side.cells:
                toward = +1 if cell.col % 2 == 0 else -1  # 左页 +x、右页 -x，均指向书脊
                dx = round(toward * creep, 3)
                nx = round(cell.x_mm + dx, 3)
                sx = _spine_x(side.cells, cell)
                codes: list[str] = []

                if creep > params.max_offset + TOL_MM:
                    codes.append("offset_exceeded")
                    sheet_diags.append(_diag(
                        "offset_exceeded",
                        f"第{i}张（由外到内第{i}层）补偿量 {creep:.3f}mm 超过最大允许偏移 "
                        f"{params.max_offset:g}mm，超限 {creep - params.max_offset:.3f}mm",
                        sheet=i,
                    ))

                # 补偿后带出血矩形（trim 四周 + bleed）须在纸张与可印区域内
                bx0, by0 = nx - bleed, cell.y_mm - bleed
                bx1, by1 = nx + cw + bleed, cell.y_mm + ch + bleed
                if (bx0 < px0 - TOL_MM or bx1 > px1 + TOL_MM
                        or by0 < py0 - TOL_MM or by1 > py1 + TOL_MM
                        or bx0 < -TOL_MM or bx1 > sw + TOL_MM
                        or by0 < -TOL_MM or by1 > sh + TOL_MM):
                    codes.append("out_of_printable")
                    sheet_diags.append(_diag(
                        "out_of_printable",
                        f"第{i}张{'正面' if side_name == 'F' else '背面'}"
                        f"第{cell.page if cell.page is not None else '空白'}页补偿后带出血矩形 "
                        f"[{bx0:.3f},{by0:.3f}]-[{bx1:.3f},{by1:.3f}]mm 越出可印区域 "
                        f"[{px0:g},{py0:g}]-[{px1:g},{py1:g}]mm（含纸张边界 0~{sw:g}×{sh:g}mm）",
                        sheet=i, side=side_name, page=cell.page,
                    ))

                page_records.append(CreepPageModel(
                    sheet=i, side=side_name, row=cell.row, col=cell.col,
                    page=cell.page, unit=cell.row * (plan.cols // 2) + cell.col // 2 + 1,
                    spine_x_mm=sx, original=(cell.x_mm, cell.y_mm),
                    compensated=(nx, cell.y_mm), shift_mm=dx, diagnostics=codes,
                ))

        # 2) 正反面套准校核：正面页与背面背对背页翻转镜像后 trim 框应重合
        back_map = {(c.row, c.col): c for c in sheet.back.cells}
        for fcell in sheet.front.cells:
            if spec.flip == FlipMode.long_edge:
                bcell = back_map.get((fcell.row, plan.cols - 1 - fcell.col))
                if bcell is None:
                    continue
                # x 镜像：补偿后正面 trim 左右缘 + 背面 trim 左右缘 = kx；y 相等
                fdx = (+1 if fcell.col % 2 == 0 else -1) * creep
                bdx = (+1 if bcell.col % 2 == 0 else -1) * creep
                f_left = fcell.x_mm + fdx
                f_right = f_left + cw
                b_left = bcell.x_mm + bdx
                b_right = b_left + cw
                err_x = max(abs((f_left + b_right) - kx), abs((f_right + b_left) - kx))
                err_y = abs(fcell.y_mm - bcell.y_mm)
                # 两页法向移动量（带符号、以各自指向书脊方向为正）也应一致
                err_shift = abs(abs(fdx) - abs(bdx))
                mirror_x = f_left + b_right
            else:
                bcell = back_map.get((plan.rows - 1 - fcell.row, fcell.col))
                if bcell is None:
                    continue
                fdx = (+1 if fcell.col % 2 == 0 else -1) * creep
                bdx = (+1 if bcell.col % 2 == 0 else -1) * creep
                f_left = fcell.x_mm + fdx
                b_left = bcell.x_mm + bdx
                f_lo, f_hi = fcell.y_mm, fcell.y_mm + ch
                b_lo, b_hi = bcell.y_mm, bcell.y_mm + ch
                err_x = abs(f_left - b_left)  # 短边翻转 x 不变
                err_y = max(abs((f_lo + b_hi) - ky), abs((f_hi + b_lo) - ky))
                err_shift = abs(abs(fdx) - abs(bdx))
                mirror_x = f_left
            mismatch = err_x > TOL_MM or err_y > TOL_MM or err_shift > TOL_MM
            # 只在至少一侧为真实页时报告（全空白格对不影响印张）
            if mismatch and (fcell.page is not None or bcell.page is not None):
                sheet_diags.append(_diag(
                    "register_mismatch",
                    f"第{i}张正反面套准错位：正面第{fcell.page if fcell.page is not None else '空白'}页 "
                    f"(row={fcell.row},col={fcell.col}) 与背面第"
                    f"{bcell.page if bcell.page is not None else '空白'}页 "
                    f"(row={bcell.row},col={bcell.col}) 镜像偏差 "
                    f"Δx={err_x:.3f}mm Δy={err_y:.3f}mm "
                    f"Δshift={err_shift:.3f}mm（镜像轴 x={mirror_x:.3f}mm）",
                    sheet=i, side="F/B", page=fcell.page,
                ))
                for rec in page_records:
                    if rec.sheet == i and rec.page in (fcell.page, bcell.page) \
                            and rec.page is not None:
                        if "register_mismatch" not in rec.diagnostics:
                            rec.diagnostics.append("register_mismatch")

        all_diags.extend(sheet_diags)
        sheet_models.append(CreepSheetModel(
            index=i, nesting=i, creep_mm=creep,
            pages=page_records, diagnostics=sheet_diags,
        ))

    error_count = sum(1 for d in all_diags if d.severity == "error")
    warning_count = sum(1 for d in all_diags if d.severity == "warning")
    formula = (f"step = 纸张厚度 {params.paper_thickness:g}mm × 压缩系数 "
               f"{params.compression_factor:g} = {step:g}mm；"
               f"第 i 张（由外到内 i=1..{plan.sheets_total}）补偿量 = step×(i-1)"
               f"（最外层 0，最内层 {step * (plan.sheets_total - 1):.3f}mm）；"
               "单元左页向 +x、右页向 -x（均指向书脊中线）")
    return CreepResponse(
        source={},  # 由调用方填入源 PDF 信息
        spec=spec,
        plan_id=plan.id,
        params=params,
        sheets_total=plan.sheets_total,
        step_mm=step,
        fore_edge_direction="书口在各折页单元外侧（左页书口朝 -x，右页书口朝 +x）；"
                            "补偿后页面整体向书脊中线移动，书口侧留出三面裁切余量",
        binding_zone=binding_zone,
        sheets=sheet_models,
        diagnostics=all_diags,
        error_count=error_count,
        warning_count=warning_count,
        formula=formula,
        notes=[
            "坐标原点在纸张左下角，x 向右（书脊法向）、y 向上，单位 mm",
            "纸张按骑马订由外到内 1→N 套叠；最外层补偿 0，向内每张增加一个步距",
            "original/compensated 为页面 trim 框左下角；shift_mm 为向书脊方向的法向补偿量",
            "诊断不阻断返回：offset_exceeded=补偿超限，out_of_printable=页面越出可印区域，"
            "register_mismatch=正反面套准错位，binding_out_of_bounds=装订区标记越界",
            "预览与导出均不修改原候选方案（/api/plans 结果不变）；未提供纸张厚度时"
            "/api/ticket 与 /api/pdf 保持原拼版结果；胶装不做爬移补偿",
        ],
    )


def compensated_cells(report: CreepResponse, sheet_index: int,
                      side: str, cells: list[CellModel]) -> list[CellModel]:
    """按爬移报告把某张某面的单元格复制为补偿后坐标（不改动原方案对象）。"""
    sm = next((s for s in report.sheets if s.index == sheet_index), None)
    if sm is None:
        return [c.model_copy() for c in cells]
    shifts = {(p.row, p.col): p.shift_mm for p in sm.pages if p.side == side}
    out = []
    for c in cells:
        dx = shifts.get((c.row, c.col), 0.0)
        out.append(c.model_copy(update={"x_mm": round(c.x_mm + dx, 3)}))
    return out
