"""拼版 PDF 生成：ReportLab 绘制标记层，pypdf 置入源页面。"""
from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .creep import CreepResponse, compensated_cells
from .engine import compute_mark_geometry  # 共享几何：工单与 PDF 标线一致
from .schemas import CellModel, JobSpec, PlanModel, SheetModel

MM = 72.0 / 25.4

# 内置中文 CID 字体（无需外部字体文件）
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
CJK = "STSong-Light"


def _pt(v_mm: float) -> float:
    return v_mm * MM


def _draw_creep_annotations(c: canvas.Canvas, spec: JobSpec, plan: PlanModel,
                            sheet: SheetModel, draw_cells: list[CellModel],
                            orig_cells: list[CellModel],
                            report: CreepResponse) -> None:
    """校样上的爬移标注：装订区边界、书口方向、逐单元/逐张偏移量。"""
    sm = next((s for s in report.sheets if s.index == sheet.index), None)
    creep = sm.creep_mm if sm else 0.0
    cw = draw_cells[0].w_mm
    ch = draw_cells[0].h_mm
    half = report.params.binding_width / 2.0
    gy0 = min(cell.y_mm for cell in draw_cells)
    gy1 = max(cell.y_mm for cell in draw_cells) + ch
    ext = _pt(5)

    # 装订区边界（蓝色点线，沿书脊中线两侧，伸出拼版区 5mm）
    c.saveState()
    c.setStrokeColorRGB(0.1, 0.25, 0.8)
    c.setLineWidth(0.5)
    c.setDash([2, 2], 0)
    zone = {z["unit"]: z for z in report.binding_zone}
    for cell in draw_cells:
        if cell.col % 2 != 0:
            continue
        unit = cell.row * (plan.cols // 2) + cell.col // 2 + 1
        z = zone.get(unit)
        if not z:
            continue
        for bx in (z["left"], z["right"]):
            xp = _pt(bx)
            c.line(xp, _pt(gy0) - ext, xp, _pt(gy1) + ext)
    c.restoreState()

    # 书口方向：最上行每个单元外侧画箭头（左页书口朝左、右页书口朝右），
    # 箭头取补偿后书口位置，直观看出补偿后书口内移
    c.saveState()
    c.setStrokeColorRGB(0.85, 0.35, 0.0)
    c.setFillColorRGB(0.85, 0.35, 0.0)
    c.setLineWidth(0.6)
    c.setFont(CJK, 6)
    top_row = max(cc.row for cc in draw_cells)
    ay = _pt(gy1) + _pt(9)
    arr = _pt(2.2)
    for cell in draw_cells:
        if cell.row != top_row:
            continue
        # 左页书口在 trim 左缘，右页书口在 trim 右缘（补偿后均向书脊中线内移）
        if cell.col % 2 == 0:
            x = _pt(cell.x_mm)
            c.line(x + arr, ay, x - arr, ay)
            c.line(x - arr, ay, x - arr * 0.2, ay + arr * 0.4)
            c.line(x - arr, ay, x - arr * 0.2, ay - arr * 0.4)
            c.drawString(x - _pt(14), ay + _pt(2), "书口")
        else:
            x = _pt(cell.x_mm + cw)
            c.line(x - arr, ay, x + arr, ay)
            c.line(x + arr, ay, x + arr * 0.2, ay + arr * 0.4)
            c.line(x + arr, ay, x + arr * 0.2, ay - arr * 0.4)
            c.drawString(x + _pt(2), ay + _pt(2), "书口")
    c.restoreState()

    # 偏移量：在最下行每条书脊中线旁标注本张补偿量；为 0 时仍标注"爬移 0"
    c.saveState()
    c.setFillColorRGB(0.1, 0.25, 0.8)
    c.setFont(CJK, 6)
    folds = sorted(z["center_x"] for z in report.binding_zone if z["row"] == 0)
    for fx in folds:
        c.drawString(_pt(fx) + _pt(1.5), _pt(gy0) - _pt(8),
                     f"爬移 {creep:.3f}mm")
    c.restoreState()


def _marks_page(spec: JobSpec, plan: PlanModel, sheet: SheetModel,
                side_cells: list[CellModel], side_name: str,
                draw_cells: list[CellModel] | None = None,
                fold_fixed: list[float] | None = None,
                creep_report: CreepResponse | None = None) -> bytes:
    """绘制一张纸某一面的标记层：裁切线、套准标记、帖码、咬口、折叠线。

    启用爬移补偿时 draw_cells 为补偿后单元格（页面/裁切标记按此绘制），
    fold_fixed 为不随补偿移动的书脊中线，creep_report 提供装订区/偏移标注。
    """
    if draw_cells is None:
        draw_cells = side_cells
    buf = io.BytesIO()
    sw, sh = _pt(spec.sheet_width), _pt(spec.sheet_height)
    c = canvas.Canvas(buf, pagesize=(sw, sh))
    c.setLineWidth(0.4)

    # 咬口示意带
    g = _pt(spec.gripper)
    c.setFillGray(0.85)
    c.setStrokeGray(0.4)
    edge = spec.gripper_edge.value
    c.setFont(CJK, 7)
    if edge == "bottom":
        c.rect(0, 0, sw, g, stroke=0, fill=1)
        c.setFillGray(0.2)
        c.drawCentredString(sw / 2, g / 2 - 2, f"咬口 {spec.gripper:g}mm")
    elif edge == "top":
        c.rect(0, sh - g, sw, g, stroke=0, fill=1)
        c.setFillGray(0.2)
        c.drawCentredString(sw / 2, sh - g / 2 - 2, f"咬口 {spec.gripper:g}mm")
    elif edge == "left":
        c.rect(0, 0, g, sh, stroke=0, fill=1)
    else:
        c.rect(sw - g, 0, g, sh, stroke=0, fill=1)

    if draw_cells:
        geo = compute_mark_geometry(spec, draw_cells, fold_fixed=fold_fixed)
        gx0, gy0, gx1, gy1 = (_pt(v) for v in geo["bbox"])

        # 外框四角裁切角线（trim corner ticks）
        tick, off = _pt(5), _pt(1.5)
        c.setStrokeGray(0)
        for cx, sx in ((gx0, -1), (gx1, 1)):
            for cy, sy in ((gy0, -1), (gy1, 1)):
                c.line(cx + sx * off, cy, cx + sx * (off + tick), cy)
                c.line(cx, cy + sy * off, cx, cy + sy * (off + tick))

        # 纵向外部裁切线（实线，贯通全纸；先外部裁切、再折叠）
        c.setLineWidth(0.8)
        for x in geo["ext_v"]:
            xp = _pt(x)
            c.line(xp, 0, xp, sh)
        c.setLineWidth(0.4)

        # 单元间/行间废边裁切线（虚线，贯通整个拼版区；书脊中线绝不画裁切线）
        c.setDash([4, 3], 0)
        for x in geo["cut_v"]:
            xp = _pt(x)
            c.line(xp, gy0, xp, gy1)
        for y in geo["cut_h"]:
            yp = _pt(y)
            c.line(gx0, yp, gx1, yp)
        c.setDash()

        # 折叠线：每个折页单元书脊中线（点划线，伸出拼版区外 5mm，仅折叠用）
        c.setDash([10, 3], 0)
        c.setLineWidth(0.7)
        ext = _pt(5)
        for x in geo["fold_v"]:
            xp = _pt(x)
            c.line(xp, gy0 - ext, xp, gy1 + ext)
        c.setDash()
        c.setLineWidth(0.4)

        # 爬移校样标注：装订区边界、书口方向、逐张偏移量
        if creep_report is not None:
            _draw_creep_annotations(c, spec, plan, sheet, draw_cells,
                                    side_cells, creep_report)

        # 套准标记（十字+圆）：拼版区四边中点外侧
        reg_r = _pt(3)
        reg_off = _pt(6)
        for rx, ry in ((gx0 - reg_off, (gy0 + gy1) / 2), (gx1 + reg_off, (gy0 + gy1) / 2),
                       ((gx0 + gx1) / 2, gy0 - reg_off), ((gx0 + gx1) / 2, gy1 + reg_off)):
            c.circle(rx, ry, reg_r, stroke=1, fill=0)
            c.line(rx - reg_r - _pt(1.5), ry, rx + reg_r + _pt(1.5), ry)
            c.line(rx, ry - reg_r - _pt(1.5), rx, ry + reg_r + _pt(1.5))

        # 帖码（书脊侧阶梯黑块，配帖时核对）
        if plan.binding.value == "perfect":
            step = _pt(6)
            bh = _pt(4)
            top = gy1 - _pt(4)
            y = top - (sheet.signature - 1) * (bh + _pt(1)) - bh
            c.setFillGray(0)
            c.rect(gx0 - _pt(2), y, _pt(1.5), bh, stroke=0, fill=1)

    # 文字标注：帖号/张号/正反面/开数 + 标线图例（先外部裁切、再沿中线折叠）
    # 张号分母按本帖实际张数（混合帖各帖张数不同）；混合帖标注本帖页数
    c.setFillGray(0)
    c.setFont(CJK, 7)
    side_cn = "正面" if side_name == "F" else "背面"
    sig_sheet_total = sum(1 for s in plan.sheets if s.signature == sheet.signature)
    sig_desc = ""
    if plan.signature_plan:
        sig_desc = f"·{plan.signature_plan[sheet.signature - 1].pages}页"
    creep_desc = ""
    if creep_report is not None:
        sm = next((s for s in creep_report.sheets if s.index == sheet.index), None)
        v = sm.creep_mm if sm else 0.0
        creep_desc = (f"  爬移补偿 {v:.3f}mm（厚度{creep_report.params.paper_thickness:g}"
                      f"×系数{creep_report.params.compression_factor:g}，装订区"
                      f"{creep_report.params.binding_width:g}mm）；蓝点线=装订区边界 "
                      f"橙箭头=书口方向；")
    label = (f"帖 {sheet.signature}/{plan.signatures}{sig_desc}  "
             f"张 {sheet.sheet_in_signature}/{sig_sheet_total}  "
             f"{side_cn}  {plan.cols}×{plan.rows}开 rot{plan.rotation}  "
             f"flip={spec.flip.value}；{creep_desc}实线=外部裁切 虚线=废边裁切 "
             f"点划线=折叠（先外部裁切，再沿中线折叠，中线禁裁）")
    c.drawString(_pt(8), _pt(spec.gripper) + _pt(2) if edge == "bottom" else _pt(2), label)

    c.showPage()
    c.save()
    return buf.getvalue()


def _cell_ctm(cell: CellModel, bleed_pt: float, fw_pt: float, fh_pt: float) -> tuple:
    """源页面置入单元格的变换矩阵 (a,b,c,d,e,f)，先旋转后平移，对齐 trim 框。"""
    X, Y = _pt(cell.x_mm), _pt(cell.y_mm)
    b = bleed_pt
    rot = cell.rotation % 360
    if rot == 0:
        return (1, 0, 0, 1, X - b, Y - b)
    if rot == 90:
        return (0, 1, -1, 0, X + b + fh_pt, Y - b)
    if rot == 180:
        return (-1, 0, 0, -1, X + b + fw_pt, Y + b + fh_pt)
    if rot == 270:
        return (0, -1, 1, 0, X - b, Y + b + fw_pt)
    raise ValueError(f"不支持的旋转角度 {cell.rotation}")


def build_imposed_pdf(src_bytes: bytes, spec: JobSpec, plan: PlanModel,
                      src_has_bleed: bool, creep_report: CreepResponse | None = None) -> bytes:
    """输出拼版 PDF。creep_report 非空时按补偿后坐标置入页面并加爬移校样标注。"""
    reader = PdfReader(io.BytesIO(src_bytes))
    src0 = reader.pages[0]
    fw_pt = float(src0.mediabox.width) - (2 * _pt(spec.bleed) if src_has_bleed else 0)
    fh_pt = float(src0.mediabox.height) - (2 * _pt(spec.bleed) if src_has_bleed else 0)
    bleed_pt = _pt(spec.bleed) if src_has_bleed else 0.0

    # 书脊中线物理位置不随爬移移动：始终取原方案首面网格
    fold_fixed = None
    if creep_report is not None:
        orig = plan.sheets[0].front.cells
        fold_fixed = sorted({cell.x_mm for cell in orig if cell.col % 2 == 1})

    writer = PdfWriter()
    for sheet in plan.sheets:
        for side_name, side in (("F", sheet.front), ("B", sheet.back)):
            draw_cells = side.cells
            if creep_report is not None:
                draw_cells = compensated_cells(creep_report, sheet.index,
                                               side_name, side.cells)
            marks = PdfReader(io.BytesIO(
                _marks_page(spec, plan, sheet, side.cells, side_name,
                            draw_cells=draw_cells, fold_fixed=fold_fixed,
                            creep_report=creep_report))).pages[0]
            for cell in draw_cells:
                if cell.page is None:
                    continue
                ctm = _cell_ctm(cell, bleed_pt, fw_pt, fh_pt)
                marks.merge_transformed_page(
                    reader.pages[cell.page - 1], Transformation(ctm))
            writer.add_page(marks)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
