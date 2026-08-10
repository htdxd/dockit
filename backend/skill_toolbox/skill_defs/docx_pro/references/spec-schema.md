# docx_pro 生成规格 Schema（work/spec.json）

`build_docx.py` 只读取这个 JSON。所有字段可选，脚本按复杂度档位取默认。

```json
{
  "title": "文档标题",
  "subtitle": "副标题（可选）",
  "author": "作者",
  "date": "2026-08-05",
  "language": "zh-CN",

  "complexity": "standard",
  "scene": "corporate",

  "cover": {
    "recipe": "R1",
    "subtitle": "副题",
    "metaLines": ["Prepared for: Acme", "2026"],
    "dark": false
  },

  "summary": "摘要文字（标准档显示在目录前）",

  "sections": [
    {
      "heading": "第一章 引言",
      "level": 1,
      "paragraphs": [
        {"text": "正文内容", "style": "body"},
        {"text": "重点强调", "style": "body", "bold": true}
      ]
    }
  ],

  "tables": [
    {
      "caption": "表 1 能力对照",
      "headers": ["能力", "状态"],
      "rows": [["表格", "通过"], ["图片", "通过"]],
      "widths_pct": [50, 50]
    }
  ],

  "images": [
    {
      "caption": "图 1 流程",
      "source": "sources/diagram.png",
      "width_mm": 120
    }
  ],

  "formulas": [
    {"latex": "E = mc^2", "caption": "质能方程"}
  ],

  "page_numbering": {
    "front_matter": "roman",
    "body_start": 1
  },

  "footer": "PAGE",
  "header": "文档名"
}
```

## 顺序 block 规格（阶段 4，优先使用）

`blocks` 取代旧的全局尾部数组：图片、表格、公式可以在任意 section/段落
之间按顺序插入（如"第 2 段后插图"）。`sections`/`tables`/`images`/`formulas`
仍是兼容字段；提供 `blocks` 时忽略旧字段。

```json
{
  "title": "报告",
  "complexity": "standard",
  "blocks": [
    {"type": "heading", "text": "第一章 引言", "level": 1},
    {"type": "paragraph", "text": "第一段正文", "style": "body"},
    {"type": "image", "source": "work/materials/<id>/assets/fig1.png", "caption": "图 1 流程", "width_mm": 120},
    {"type": "paragraph", "text": "引用上面的图后继续正文", "style": "body"},
    {"type": "table", "headers": ["能力", "状态"], "rows": [["表格", "通过"]], "caption": "表 1 能力对照"},
    {"type": "formula", "latex": "E = mc^2", "caption": "质能方程"}
  ]
}
```

block 类型与字段：

| type | 必需字段 | 说明 |
|---|---|---|
| `heading` | `text`, `level` | 真 Heading 样式（进目录） |
| `paragraph` | `text` | `style`=body/quote/code/center，`bold` 可选 |
| `image` | `source` | `caption`/`width_mm` 可选；保持比例，禁止双写宽高 |
| `table` | `headers`, `rows` | `caption`/`widths_pct` 可选；表头跨页重复 |
| `formula` | `latex` | `caption` 可选；复杂公式 matplotlib PNG 兜底 |

## 语义约定

- `sections[].level`：1=Heading1 … 3=Heading3。只有 level≤3 进入目录。
- `paragraphs[].style`：`body`（正文，2 字缩进）/ `heading` / `quote` / `code` / `center`。
- `complexity=simple`：忽略 cover/toc/page_numbering；不注入目录。
- `complexity=gongwen`：scene 强制 `gongwen`，section 标题序列按 GB/T 9704 渲染，页码 `-X-`。
- 图片 `width_mm` 缺省：按页面可用宽度 ×0.9。
- 公式 latex 含 `\frac`、`\sum`、`\sqrt`、矩阵或分段 → 脚本用 matplotlib 渲染 PNG 嵌入（需 matplotlib 可用）；否则 OMML 文本注入。
- 材料里的图片/表格/公式从 `[MATERIALS]` 横幅的共享 IR 引用（`work/materials/<id>/assets/`），先写 ContentPlan 再按 blocks 摆放。
