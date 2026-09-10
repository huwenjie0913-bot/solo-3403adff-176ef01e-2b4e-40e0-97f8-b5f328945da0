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
| POST | `/api/plans` | 枚举可行开数/横竖放置方案（含无解原因），按纸张用量→空白页→裁切刀数排序 |
| POST | `/api/ticket` | 生成 JSON 工单（默认最优方案，可用选择器指定开数） |
| POST | `/api/pdf` | 输出拼版 PDF（裁切线、套准标记、帖码、咬口标注） |

三个 POST 接口均为 `multipart/form-data`：

- `file`：源 PDF（页面尺寸须等于成品尺寸，或成品尺寸+四边出血，容差 1.5mm）
- `spec`：JobSpec JSON 字符串
- `rotation` / `cols` / `rows`（可选，仅 ticket/pdf）：指定某个候选方案

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
  "pages_per_signature": 16,    // 每帖页数（仅胶装，须为 4 的倍数）
  "max_layouts": 8
}
```

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
```

## 校验与无解原因

- **源文件**：页尺寸须全册一致，且等于成品尺寸或成品+四边出血，否则 422 并给出实际/期望尺寸。
- **可印区域**：拼版区须落在扣除咬口后的可印区域内，放不下最小折页单元（2 页宽）时给出具体尺寸。
- **纹向**：纸张丝流须平行书脊（成品高度方向）；某种放置方向不满足时该方向整体否决并说明。
- **胶装帖页数**：每帖页数须为每张纸折页单元数的整数倍，否则该开数被否决并给出建议取值。
- 无解时 `/api/plans` 的 `rejections` 逐条列出每个方向/开数被否决的具体原因。

## 拼版规则

- 一个折页单元 = 2 页宽 × 1 页高 = 4 页；列数枚举偶数，每面最多 64 页。
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
