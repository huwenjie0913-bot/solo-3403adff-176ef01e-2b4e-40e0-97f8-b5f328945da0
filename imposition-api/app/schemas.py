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
    pages_per_signature: int = Field(default=16, description="每帖页数（仅胶装有效，须为 4 的倍数）")
    max_layouts: int = Field(default=8, ge=1, le=50, description="最多返回的候选方案数")

    @model_validator(mode="after")
    def _check(self) -> "JobSpec":
        if self.binding == BindingType.perfect and self.pages_per_signature % 4 != 0:
            raise ValueError("胶装每帖页数必须是 4 的倍数")
        if self.sheet_width <= 0 or self.sheet_height <= 0:
            raise ValueError("纸张尺寸必须为正")
        return self


class LayoutSelector(BaseModel):
    """指定某个候选开数方案；不指定则取排序后的最优方案。"""

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
    sheets_per_signature: int
    capacity_pages: int              # 排版总容量页数
    blank_pages: int
    blanks: list[int]                # 空白页页码
    cuts_per_sheet: int
    grain_parallel_to_spine: bool
    printable_width_mm: float
    printable_height_mm: float
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
