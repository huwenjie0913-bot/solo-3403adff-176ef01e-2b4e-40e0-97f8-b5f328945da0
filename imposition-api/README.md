# 印前拼版方案 API（骑马订 / 胶装）

供小型印刷厂印前人员使用的本地 REST API：上传源 PDF 与工艺参数，自动枚举可行的开数与
横竖放置方案，输出每张纸正反面的页码、旋转角度、空白页、裁切折叠顺序，并可生成带
裁切线、套准标记和帖码的拼版 PDF 及供现场核对的 JSON 工单。

所有运算与文件处理均在本机完成，不依赖任何外部服务。

## 启动

```bash
cd imposition-api
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

交互式接口文档：http://127.0.0.1:8000/docs

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/plans` | 枚举可行开数/横竖放置方案（含无解原因），按总用纸→空白页→帖型数量→各帖页数差排序 |
| POST | `/api/ticket` | 生成 JSON 工单（默认最优方案，可用 plan_id 或开数选择器指定候选） |
| POST | `/api/pdf` | 输出拼版 PDF（裁切线、套准标记、帖码、咬口标注） |

三个 POST 接口均为 `multipart/form-data`：

- `file`：源 PDF（页面尺寸须等于成品尺寸，或成品尺寸+四边出血，容差 1.5mm）
- `spec`：JobSpec JSON 字符串
- `plan_id`（可选，仅 ticket/pdf）：按候选方案 ID 指定方案（优先于开数选择器）
- `rotation` / `cols` / `rows`（可选，仅 ticket/pdf）：指定某个候选开数方案

### JobSpec 参数（单位 mm）

```json
{
  "finished_width": 105,        // 成品宽
  "finished_height": 148,       // 成品高（书脊平行此方向）
  "sheet_width": 320,           // 纸张宽
  "sheet_height": 450,          // 纸张高
  "grain": "long_edge",         // 纹向：long_edge 长边纹 / short_edge 短边纹
  "bleed": 3,                   // 出血
  "gripper": 10,                // 咬口宽度
  "gripper_edge": "bottom",     // 咬口所在纸边 top/bottom/left/right
  "flip": "long_edge",          // 双面翻转：long_edge 长边 / short_edge 短边
  "binding": "saddle",          // 装订：saddle 骑马订 / perfect 胶装
  "pages_per_signature": 16,    // 每帖页数（仅胶装固定帖模式，须为 4 的倍数）
  "allowed_signature_pages": null,  // 可选：允许的每帖页数候选列表（仅胶装，均须 4 的倍数）；
                                    // 设置后启用混合帖规划并忽略 pages_per_signature
  "max_signatures": null,       // 可选：最大帖数（仅混合帖，默认 50）
  "max_blank_pages": null,      // 可选：空白页上限（仅混合帖，默认不限）
  "max_layouts": 8
}
```

### 胶装混合帖规划

设置 `allowed_signature_pages` 后（仅胶装有效），按实际页数与候选开数组合配帖方案：

- **约束**：各帖页数为 4 的倍数，且能被当前开数的单张纸容量（每面折页单元数 × 4 页）整除；
  帖数不超过 `max_signatures`，空白页不超过 `max_blank_pages`；
  空白页全部落在末帖（末帖至少 1 个真实页，前帖不含空白）。
- **排序**：总用纸 → 空白页 → 帖型数量 → 各帖页数差。候选按容量升序精确枚举
  （单一/双帖型直接求解，三种及以上帖型在节点预算内搜索），每个开数只保留前
  `max_layouts` 个候选；组合空间超预算时该开数被显式否决并说明，
  绝不把未完整搜索的结果当作候选返回。
- **候选结果**：方案 ID 形如 `rot0-2x2-mix16x3+8`（开数 + 配帖构成）；
  `signature_plan` 给出逐帖页码范围、容量、用纸数、空白页位置和帖码；
  `signature_types`/`signature_spread` 为帖型数量与各帖页数差。
- **工单/PDF**：`/api/ticket`、`/api/pdf` 可用 `plan_id` 指定混合帖候选，
  按各帖容量生成页码网格；PDF 阶梯帖码、工单配帖顺序与候选结果均按帖序号 1→N 一致。
- **无解时**：`rejections` 指出冲突的约束——帖数上限不足（`max_signatures`）、
  空白页超限（`max_blank_pages`，附最小可实现空白页数）或
  帖页数不被单张容量整除（`allowed_signature_pages`）。
- 未设置 `allowed_signature_pages` 时保持固定帖行为；骑马订忽略上述三个参数。

### curl 示例

```bash
# 生成样例源文件（32 页 105×148mm）
python3 scripts/make_sample.py 32 105 148 /tmp/src.pdf

SPEC='{"finished_width":105,"finished_height":148,"sheet_width":320,"sheet_height":450,
"grain":"long_edge","bleed":3,"gripper":10,"gripper_edge":"bottom","flip":"long_edge",
"binding":"saddle","pages_per_signature":16,"max_layouts":8}'

# 枚举方案
curl -s -F "file=@/tmp/src.pdf" -F "spec=$SPEC" http://127.0.0.1:8000/api/plans

# JSON 工单（指定 2×2 正放）
curl -s -F "file=@/tmp/src.pdf" -F "spec=$SPEC" -F "rotation=0" -F "cols=2" -F "rows=2" \
  http://127.0.0.1:8000/api/ticket

# 拼版 PDF
curl -s -F "file=@/tmp/src.pdf" -F "spec=$SPEC" \
  http://127.0.0.1:8000/api/pdf -o imposed.pdf

# 胶装混合帖：50 页，允许 16/8 页帖
python3 scripts/make_sample.py 50 105 148 /tmp/src50.pdf
SPEC_MIX='{"finished_width":105,"finished_height":148,"sheet_width":320,"sheet_height":450,
"grain":"long_edge","bleed":3,"gripper":10,"gripper_edge":"bottom","flip":"long_edge",
"binding":"perfect","allowed_signature_pages":[16,8],"max_signatures":12,"max_blank_pages":8}'

# 枚举混合帖候选（signature_plan 含逐帖页码范围/用纸/空白页/帖码）
curl -s -F "file=@/tmp/src50.pdf" -F "spec=$SPEC_MIX" http://127.0.0.1:8000/api/plans

# 按候选 ID 出工单 / 拼版 PDF
curl -s -F "file=@/tmp/src50.pdf" -F "spec=$SPEC_MIX" -F "plan_id=rot0-2x2-mix16x3+8" \
  http://127.0.0.1:8000/api/ticket
curl -s -F "file=@/tmp/src50.pdf" -F "spec=$SPEC_MIX" -F "plan_id=rot0-2x2-mix16x3+8" \
  http://127.0.0.1:8000/api/pdf -o imposed_mix.pdf
```

## 校验与无解原因

- **源文件**：页尺寸须全册一致，且等于成品尺寸或成品+四边出血，否则 422 并给出实际/期望尺寸。
- **可印区域**：每页的带出血矩形（成品尺寸 + 四周出血）须完整落在扣除咬口后的
  可印区域内，放不下最小折页单元（含出血）时给出具体尺寸对比。
- **纹向**：纸张丝流须平行书脊（成品高度方向）；某种放置方向不满足时该方向整体否决并说明。
- **胶装帖页数**：每帖页数须为每张纸折页单元数的整数倍，否则该开数被否决并给出建议取值。
- 无解时 `/api/plans` 的 `rejections` 逐条列出每个方向/开数被否决的具体原因。

## 拼版规则

- 一个折页单元 = 2 页宽 × 1 页高 = 4 页；列数枚举偶数，每面最多 64 页。
- 单元内两页在书脊中线处紧贴，**中线仅为折叠线**；版面横向靠侧规边对齐，
  裁切只包含：① 纵向——单元间废边槽 + 1 条右侧外部裁切线（实线，贯通全纸）；
  ② 横向——行间废边槽（虚线，沿废边两条边界）。每个废边槽记 1 刀。
  工单步骤与 PDF 图例均表达"先外部裁切、再沿中线折叠"，不会拆开折页单元。
- 每页的带出血矩形（成品 + 四周出血）必须完整落在扣除咬口后的可印区域内，
  否则该方向/开数被否决并说明原因。
- 骑马订：全册按单元由外向内套页（第 1 张正面为 [末页, 第1页, …]）。
- 胶装：每帖内部同样按由外向内折手，帖间按帖码配帖；书脊侧绘制阶梯帖码黑块。
- 背面按双面翻转方式镜像：长边翻转左右镜像；短边翻转上下镜像且背面内容旋转 180°。
- 排序：纸张用量 → 空白页数 → 裁切刀数 → 每面页数（大开数优先）。

## 测试

```bash
python3 scripts/smoke_test.py   # 端到端冒烟测试（30+ 项断言，无需启动服务）
```

## 目录结构

```
app/
  main.py         FastAPI 入口与接口
  schemas.py      Pydantic 请求/响应模型
  engine.py       开数枚举、页码排布、校验与排序
  pdf_builder.py  ReportLab 标记层 + pypdf 页面置入
scripts/
  make_sample.py  生成测试用源 PDF
  smoke_test.py   冒烟测试
```
