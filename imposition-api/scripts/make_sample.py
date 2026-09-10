"""生成测试用源 PDF：每页绘制页码与成品框，便于目视核对拼版结果。

用法: python3 scripts/make_sample.py [页数] [宽mm] [高mm] [输出路径]
"""
import sys

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def make_sample(pages: int = 32, w_mm: float = 105.0, h_mm: float = 148.0,
                out: str = "sample_src.pdf") -> str:
    c = canvas.Canvas(out, pagesize=(w_mm * mm, h_mm * mm))
    for i in range(1, pages + 1):
        c.setStrokeGray(0.6)
        c.rect(3 * mm, 3 * mm, (w_mm - 6) * mm, (h_mm - 6) * mm)  # 模拟出血框
        c.setFillGray(0)
        c.setFont("Helvetica-Bold", 48)
        c.drawCentredString(w_mm / 2 * mm, h_mm / 2 * mm, str(i))
        c.setFont("Helvetica", 8)
        c.drawString(5 * mm, 5 * mm, f"page {i} / {pages}")
        c.showPage()
    c.save()
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    path = make_sample(
        pages=int(args[0]) if len(args) > 0 else 32,
        w_mm=float(args[1]) if len(args) > 1 else 105.0,
        h_mm=float(args[2]) if len(args) > 2 else 148.0,
        out=args[3] if len(args) > 3 else "sample_src.pdf",
    )
    print(f"已生成 {path}")
