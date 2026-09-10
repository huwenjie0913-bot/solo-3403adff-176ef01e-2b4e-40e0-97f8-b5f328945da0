"""请求/响应数据模型（全部使用毫米为单位）。"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class BindingType(str, Enum):
    saddle = "saddle"    # 骑马订
    perfect = "perfect"  # 胶装


class FlipMode(str, Enum):
    long_edge = "long_edge"    # 长边翻转（左右翻）
    short_edge = "short_edge"  # 短边翻转（上下翻）


class GrainDirection(str, Enum):
    long_edge = "long_edge"    # 长边纹：丝流平行纸张长边
    short_edge = "short_edge"  # 短边纹：丝流平行纸张短边


class GripperEdge(str, Enum):
    top = "top"
    bottom = "bottom"
    left = "left"
    right = "right"


class JobSpec(BaseModel):
    """拼版工单参数（长度单位均为 mm）。"""

    finished_width: float = Field(gt=0, description="成品宽度")
    finished_height: float = Field(gt=0, description="成品高度")
    sheet_width: float = Field(gt=0, description="纸张宽度")
    sheet_height: float = Field(gt=0, description="纸张高度")
    grain: GrainDirection = Field(description="纸张纹向（丝流方向）")
    bleed: float = Field(default=3.0, ge=0, description="出血")
    gripper: float = Field(default=10.0, ge=0, description="咬口宽度")
    gripper_edge: GripperEdge = Field(default=GripperEdge.bottom, description="咬口所在纸边")
    flip: FlipMode = Field(default=FlipMode.long_edge, description="双面翻转方式")
    binding: BindingType = Field(description="装订类型")
    pages_per_signature: int = Field(default=16, description="每帖页数（仅胶装固定帖模式，须为 4 的倍数）")
    allowed_signature_pages: Optional[list[int]] = Field(
        default=None,
        description="允许的每帖页数候选（仅胶装，均须为 4 的倍数）；"
                    "设置后启用混合帖规划并忽略 pages_per_signature，未设置时保持固定帖行为",
    )
    max_signatures: Optional[int] = Field(default=None, ge=1, le=200,
                                          description="最大帖数（仅混合帖，默认 50）")
    max_blank_pages: Optional[int] = Field(default=None, ge=0,
                                           description="空白页上限（仅混合帖，默认不限）")
    max_layouts: int = Field(default=8, ge=1, le=50, description="最多返回的候选方案数")

    @model_validator(mode="after")
    def _check(self) -> "JobSpec":
        if self.binding == BindingType.perfect and self.allowed_signature_pages is None:
            if self.pages_per_signature % 4 != 0:
                raise ValueError("胶装每帖页数必须是 4 的倍数")
        if self.binding == BindingType.perfect and self.allowed_signature_pages is not None:
            if not self.allowed_signature_pages:
                raise ValueError("allowed_signature_pages 至少包含一种每帖页数")
            bad = [p for p in self.allowed_signature_pages if p <= 0 or p % 4 != 0]
            if bad:
                raise ValueError(f"允许的每帖页数必须是 4 的倍数：{bad}")
            self.allowed_signature_pages = sorted(set(self.allowed_signature_pages), reverse=True)
        if self.sheet_width <= 0 or self.sheet_height <= 0:
            raise ValueError("纸张尺寸必须为正")
        return self

    @property
    def mixed_signatures(self) -> bool:
        """是否启用胶装混合帖规划。"""
        return self.binding == BindingType.perfect and self.allowed_signature_pages is not None


class LayoutSelector(BaseModel):
    """指定某个候选方案；不指定则取排序后的最优方案。"""

    plan_id: Optional[str] = Field(default=None, description="候选方案 ID（优先于开数选择器）")
    rotation: Optional[int] = Field(default=None, description="页面放置旋转角度 0/90")
    cols: Optional[int] = Field(default=None, description="列数")
    rows: Optional[int] = Field(default=None, description="行数")


class CellModel(BaseModel):
    row: int
    col: int
    page: Optional[int] = None      # None 表示空白页
    rotation: int                    # 该页内容放置时的旋转角度
    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float


class SheetSide(BaseModel):
    cells: list[CellModel]


class SheetModel(BaseModel):
    index: int                       # 全局纸张序号（从 1 开始）
    signature: int                   # 所属帖（从 1 开始；骑马订恒为 1）
    sheet_in_signature: int          # 帖内第几张（从 1 开始）
    front: SheetSide
    back: SheetSide


class SignaturePart(BaseModel):
    """混合帖方案中的一帖（配帖顺序即帖码顺序）。"""

    index: int                # 帖序号（从 1 开始）
    pages: int                # 本帖页数（4 的倍数，且为当前开数单张纸容量的整数倍）
    start_page: int           # 起始页码（从 1 开始）
    end_page: int             # 结束页码（含空白页）
    sheets: int               # 本帖用纸张数
    blanks: list[int]         # 本帖内空白页页码（仅末帖可能非空）
    mark: str                 # 帖码（书脊阶梯帖码级别，与 PDF 黑块位置一致）


class PlanModel(BaseModel):
    id: str
    binding: BindingType
    rotation: int                    # 0=正放 90=横放
    cols: int
    rows: int
    pages_per_side: int
    units_per_sheet: int             # 每张纸含折页单元数（1 单元 = 4 页）
    sheets_total: int                # 全册用纸张数
    signatures: int
    sheets_per_signature: int        # 单帖最多用纸张数（混合帖逐帖张数见 signature_plan）
    capacity_pages: int              # 排版总容量页数
    blank_pages: int
    blanks: list[int]                # 空白页页码
    cuts_per_sheet: int
    grain_parallel_to_spine: bool
    printable_width_mm: float
    printable_height_mm: float
    signature_types: int = 1         # 帖型数量（不同帖页数的种数）
    signature_spread: int = 0        # 各帖页数差（最大-最小）
    signature_plan: Optional[list[SignaturePart]] = None  # 混合帖逐帖明细（仅混合帖模式）
    steps: list[str]                 # 裁切折叠顺序
    sheets: list[SheetModel]


class RejectionModel(BaseModel):
    layout: str
    reason: str


class PlansResponse(BaseModel):
    source: dict
    spec: JobSpec
    plans: list[PlanModel]
    rejections: list[RejectionModel]
