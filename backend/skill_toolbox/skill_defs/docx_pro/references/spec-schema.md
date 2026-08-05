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

## 语义约定

- `sections[].level`：1=Heading1 … 3=Heading3。只有 level≤3 进入目录。
- `paragraphs[].style`：`body`（正文，2 字缩进）/ `heading` / `quote` / `code` / `center`。
- `complexity=simple`：忽略 cover/toc/page_numbering；不注入目录。
- `complexity=gongwen`：scene 强制 `gongwen`，section 标题序列按 GB/T 9704 渲染，页码 `-X-`。
- 图片 `width_mm` 缺省：按页面可用宽度 ×0.9。
- 公式 latex 含 `\frac`、`\sum`、`\sqrt`、矩阵或分段 → 脚本用 matplotlib 渲染 PNG 嵌入（需 matplotlib 可用）；否则 OMML 文本注入。
