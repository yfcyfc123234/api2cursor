# 模型定价与费用估算

管理面板「用量统计」读取仓库根目录的 `model_pricing.json`（或环境变量 `MODEL_PRICING_PATH`），按 **每 1M tokens** 的单价估算本次进程内的累计费用。

## 与 Cursor 模型名的关系

- `usage_tracker` 按 **Cursor 请求的模型名**（`client_model`）聚合 tokens。
- 匹配时先找 **模型 `id`**（与 API 模型名一致），再找 **`aliases`**：`"Cursor 显示名": "模型 id"`。

## 推荐结构（schema_version 2）：公司 → 系列 → 模型

顶层除通用字段外，使用 **`providers`** 数组，便于管理面板以树形展示；**每条模型单独写 `source_url`**，用于「打开官网核对当前价」，不再使用全文件单一来源链接。

```json
{
  "schema_version": 2,
  "currency": "USD",
  "currency_symbol": "$",
  "note": "全局说明（可选）",
  "updated_at": "2026-04-02",
  "aliases": {},
  "providers": [
    {
      "id": "moonshot",
      "name": "月之暗面 Moonshot（Kimi）",
      "series": [
        {
          "id": "kimi-k2",
          "name": "Kimi K2",
          "models": [
            {
              "id": "kimi-k2-0905-preview",
              "name": "kimi-k2-0905-preview",
              "input_per_million": 0.6,
              "output_per_million": 2.5,
              "source_url": "https://platform.kimi.com/docs/pricing/chat"
            }
          ]
        }
      ]
    }
  ]
}
```

| 层级 | 字段 | 说明 |
|------|------|------|
| 根 | `currency` / `currency_symbol` | 展示用。 |
| 根 | `note` | 全局备注（如缓存价未计入等）。 |
| 根 | `updated_at` | 人工标注的本文件更新日期。 |
| 根 | `aliases` | Cursor 名 → 模型 `id`。 |
| `providers[]` | `id`, `name` | **公司 / 供应商**（展示用）。 |
| `series[]` | `id`, `name` | **系列**（如 Kimi K2、Moonshot v1）。 |
| `models[]` | **`id`** | **必填**，与上游 API / Cursor 映射一致，用于计价匹配。 |
| `models[]` | `name` | 可选，展示用；默认可等于 `id`。 |
| `models[]` | `input_per_million` / `output_per_million` | 每 **1,000,000** tokens 的标价。 |
| `models[]` | **`source_url`** | **建议必填**：该条价格依据的网页；管理面板每条模型上有按钮跳转。 |

同一 `id` 在文件中不应重复；若重复，**以后出现的条目为准**（并打日志）。

## 旧版扁平结构（仍支持）

若未配置非空的 `providers`，则使用根上的 **`models`** 对象：`"模型id": { "input_per_million", "output_per_million", "source_url"? }`。旧文件可无 `source_url`，用量表里「定价页」列为空。

## 估算公式

`费用 = (input_tokens / 1e6) * input_per_million + (output_tokens / 1e6) * output_per_million`

未匹配到模型或未配置单价时，该模型不计入「合计预估」。

## 工作流（AI 填价 + Git + 部署）

1. 按各厂商文档为 **每个模型** 填写 `source_url`（可多条指向同一页，也可指向不同锚点/文档）。
2. 写入 **`model_pricing.json`**（勿放进被忽略的 `data/`）。
3. 部署后新文件进入镜像；若挂载覆盖，改完可在管理面板 **「重新加载定价」** 或 `POST /api/admin/pricing/reload`。

## 环境变量

| 变量 | 含义 |
|------|------|
| `MODEL_PRICING_PATH` | 定价文件路径（绝对或相对项目根）。默认 `model_pricing.json`。 |

## 免责声明

界面所示为根据本地标价的 **估算值**，结算以云厂商账单为准。
