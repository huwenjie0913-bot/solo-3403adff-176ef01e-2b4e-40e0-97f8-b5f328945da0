"""拼版核心引擎：枚举开数/横竖放置、页码排布、校验与排序。

坐标约定：原点为纸张左下角，x 向右，y 向上（与 PDF 一致），单位 mm。
一个"折页单元"= 一张纸裁折后的最小单元 = 2 页宽 × 1 页高 = 4 页。
"""
from __future__ import annotations

import math

from .schemas import (
    BindingType,
    CellModel,
    FlipMode,
    GrainDirection,
    GripperEdge,
    JobSpec,
    PlanModel,
    RejectionModel,
    SheetModel,
    SheetSide,
)

MAX_PAGES_PER_SIDE = 64  # 枚举上限，避免生成无意义的超小开数


def grain_axis(spec: JobSpec) -> str:
    """纸张纹向（丝流）沿纸张哪条边：'height' 或 'width'。"""
    long_is_height = spec.sheet_height >= spec.sheet_width
    if spec.grain == GrainDirection.long_edge:
        return "height" if long_is_height else "width"
    return "width" if long_is_height else "height"


def printable_area(spec: JobSpec) -> tuple[float, float, float, float]:
    """扣除咬口后的可印区域，返回 (x0, y0, width, height)。"""
    sw, sh, g = spec.sheet_width, spec.sheet_height, spec.gripper
    e = spec.gripper_edge
    if e == GripperEdge.bottom:
        return 0.0, g, sw, sh - g
    if e == GripperEdge.top:
        return 0.0, 0.0, sw, sh - g
    if e == GripperEdge.left:
        return g, 0.0, sw - g, sh
    return 0.0, 0.0, sw - g, sh


def _unit_pages(sig_pages: int, base: int, u: int) -> tuple[int, int, int, int]:
    """帖内第 u 个折页单元（u 从 0 计，最外层为 0）的四个页码。

    返回 (正面左, 正面右, 背面左, 背面右)，页码基于帖首页 base。
    """
    fl = base + sig_pages - 2 * u
    fr = base + 2 * u + 1
    bl = base + 2 * u + 2
    br = base + sig_pages - 2 * u - 1
    return fl, fr, bl, br


def _grid_pages(
    spec: JobSpec, total_pages: int, sig_pages: int, base: int,
    first_unit: int, n_units: int, cols: int, rows: int,
) -> tuple[dict, dict]:
    """生成一张纸正/反两面的页码网格 {(row, col): page|None}。"""
    front: dict[tuple[int, int], int | None] = {}
    back: dict[tuple[int, int], int | None] = {}
    pairs_per_row = cols // 2

    def real(p: int) -> int | None:
        return p if p <= total_pages else None

    for j in range(n_units):
        u = first_unit + j
        fl, fr, bl, br = _unit_pages(sig_pages, base, u)
        row, cp = divmod(j, pairs_per_row)
        lc, rc = 2 * cp, 2 * cp + 1  # 单元左/右两格
        front[(row, lc)] = real(fl)
        front[(row, rc)] = real(fr)
        # 背面：与正面同一物理格背对背。正面左格背后是"背面右页"，右格背后是"背面左页"
        if spec.flip == FlipMode.long_edge:
            back[(row, cols - 1 - lc)] = real(br)
            back[(row, cols - 1 - rc)] = real(bl)
        else:  # 短边翻转：上下镜像，且背面内容需旋转 180°（在单元格 rotation 中体现）
            back[(rows - 1 - row, lc)] = real(br)
            back[(rows - 1 - row, rc)] = real(bl)
    return front, back


def _cells(
    spec: JobSpec, grid: dict, cols: int, rows: int,
    cw: float, ch: float, ox: float, oy: float, rotation: int, back_side: bool,
) -> list[CellModel]:
    cells = []
    rot = rotation
    if back_side and spec.flip == FlipMode.short_edge:
        rot = (rotation + 180) % 360
    for (row, col), page in sorted(grid.items()):
        cells.append(CellModel(
            row=row, col=col, page=page, rotation=rot,
            x_mm=round(ox + col * cw, 3),
            y_mm=round(oy + (rows - 1 - row) * ch, 3),
            w_mm=cw, h_mm=ch,
        ))
    return cells


def _steps(spec: JobSpec, cols: int, rows: int, n_units: int,
           signatures: int, units_per_sig: int) -> list[str]:
    steps = []
    v, h = cols - 1, rows - 1
    steps.append(
        f"裁切：每张纸纵向切 {v} 刀、横向切 {h} 刀（先横后纵），"
        f"得到 {n_units} 个折页单元（每单元 2 页宽 × 1 页高，共 {n_units * 4} 页）"
    )
    if spec.binding == BindingType.saddle:
        steps.append(
            f"套页：将全部 {units_per_sig} 个折页单元按编号 1→{units_per_sig} 顺序套叠"
            "（1 号在最外层），沿书脊中线对齐"
        )
        steps.append("折叠：沿每个单元书脊中线对折，一次折合成册")
        steps.append("装订：沿书脊骑马钉 2~3 钉，三面裁切成品")
    else:
        steps.append(
            f"成帖：每帖 {units_per_sig} 个折页单元按编号顺序叠放，"
            f"沿书脊中线对折成帖（每帖 {spec.pages_per_signature} 页）"
        )
        steps.append(
            f"配帖：共 {signatures} 帖，按帖码 1→{signatures} 顺序配帖"
            "（核对书脊处阶梯帖码标记）"
        )
        steps.append("装订：铣背/锁线后刷胶，包封面，三面裁切成品")
    return steps


def _build_plan(spec: JobSpec, total_pages: int, rotation: int,
                cols: int, rows: int) -> PlanModel | None:
    fw, fh = spec.finished_width, spec.finished_height
    cw, ch = (fw, fh) if rotation == 0 else (fh, fw)
    n_units = (cols // 2) * rows

    if spec.binding == BindingType.saddle:
        units_needed = math.ceil(total_pages / 4)
        sheets_total = math.ceil(units_needed / n_units)
        signatures, sheets_per_sig = 1, sheets_total
        sig_pages = sheets_total * n_units * 4  # 整册即一"帖"
    else:
        sig_pages = spec.pages_per_signature
        units_per_sig = sig_pages // 4
        if units_per_sig % n_units != 0:
            return None  # 每帖页数不是每张纸单元数的整数倍，由调用方给出原因
        sheets_per_sig = units_per_sig // n_units
        signatures = math.ceil(total_pages / sig_pages)
        sheets_total = signatures * sheets_per_sig

    capacity = signatures * sig_pages
    blanks = list(range(total_pages + 1, capacity + 1))

    px0, py0, pw, ph = printable_area(spec)
    ox = px0 + (pw - cols * cw) / 2
    oy = py0 + (ph - rows * ch) / 2

    sheets: list[SheetModel] = []
    for s in range(sheets_total):
        sig_idx, s_in_sig = divmod(s, sheets_per_sig)
        base = sig_idx * sig_pages
        first_unit = s_in_sig * n_units
        front_g, back_g = _grid_pages(
            spec, total_pages, sig_pages, base, first_unit, n_units, cols, rows)
        sheets.append(SheetModel(
            index=s + 1, signature=sig_idx + 1, sheet_in_signature=s_in_sig + 1,
            front=SheetSide(cells=_cells(spec, front_g, cols, rows, cw, ch, ox, oy, rotation, False)),
            back=SheetSide(cells=_cells(spec, back_g, cols, rows, cw, ch, ox, oy, rotation, True)),
        ))

    units_per_sig = sheets_per_sig * n_units
    return PlanModel(
        id=f"rot{rotation}-{cols}x{rows}",
        binding=spec.binding, rotation=rotation, cols=cols, rows=rows,
        pages_per_side=cols * rows, units_per_sheet=n_units,
        sheets_total=sheets_total, signatures=signatures,
        sheets_per_signature=sheets_per_sig,
        capacity_pages=capacity, blank_pages=len(blanks), blanks=blanks,
        cuts_per_sheet=(cols - 1) + (rows - 1),
        grain_parallel_to_spine=True,
        printable_width_mm=pw, printable_height_mm=ph,
        steps=_steps(spec, cols, rows, n_units, signatures, units_per_sig),
        sheets=sheets,
    )


def enumerate_plans(spec: JobSpec, total_pages: int) -> tuple[list[PlanModel], list[RejectionModel]]:
    """枚举所有可行开数与横竖放置，返回 (方案列表, 否决原因列表)。"""
    plans: list[PlanModel] = []
    rejections: list[RejectionModel] = []
    fw, fh = spec.finished_width, spec.finished_height
    _, _, pw, ph = printable_area(spec)
    axis = grain_axis(spec)

    for rotation in (0, 90):
        cw, ch = (fw, fh) if rotation == 0 else (fh, fw)
        orient = "正放" if rotation == 0 else "横放(旋转90°)"
        # 纹向校验：书脊平行成品高度方向；页面旋转后其高度方向随之改变
        need_axis = "height" if rotation == 0 else "width"
        if axis != need_axis:
            rejections.append(RejectionModel(
                layout=f"{orient}",
                reason=f"纸张纹向平行纸张{'长' if axis == 'height' else '短'}边"
                       f"（沿纸张{'高' if axis == 'height' else '宽'}度方向），"
                       f"页面{orient}后纹向不平行书脊",
            ))
            continue
        max_cols = int(pw // cw)
        max_rows = int(ph // ch)
        if max_cols < 2 or max_rows < 1:
            rejections.append(RejectionModel(
                layout=f"{orient}",
                reason=f"可印区域 {pw:.1f}×{ph:.1f}mm 放不下一个折页单元"
                       f"（需 {2 * cw:.1f}×{ch:.1f}mm，已扣除咬口 {spec.gripper}mm）",
            ))
            continue
        cols = 2
        while cols <= max_cols and cols <= MAX_PAGES_PER_SIDE:
            rows = 1
            while rows <= max_rows and cols * rows <= MAX_PAGES_PER_SIDE:
                plan = _build_plan(spec, total_pages, rotation, cols, rows)
                if plan is None:
                    rejections.append(RejectionModel(
                        layout=f"{orient} {cols}×{rows}（每面 {cols * rows} 页）",
                        reason=f"胶装每帖 {spec.pages_per_signature} 页 = "
                               f"{spec.pages_per_signature // 4} 个折页单元，不是每张纸 "
                               f"{(cols // 2) * rows} 个单元的整数倍；"
                               f"请调整每帖页数（如 {(cols // 2) * rows * 4} 的倍数）",
                    ))
                else:
                    plans.append(plan)
                rows += 1
            cols += 2  # 列数必须为偶数，折页单元才能成对

    if not plans and not rejections:
        rejections.append(RejectionModel(layout="-", reason="无可枚举的开数"))

    # 排序：纸张用量 → 空白页数 → 裁切刀数 → 每面页数（大开数优先）
    plans.sort(key=lambda p: (p.sheets_total, p.blank_pages, p.cuts_per_sheet, -p.pages_per_side))
    return plans[: spec.max_layouts], rejections


def find_plan(plans: list[PlanModel], rotation: int | None,
              cols: int | None, rows: int | None) -> PlanModel | None:
    for p in plans:
        if rotation is not None and p.rotation != rotation:
            continue
        if cols is not None and p.cols != cols:
            continue
        if rows is not None and p.rows != rows:
            continue
        return p
    return None
