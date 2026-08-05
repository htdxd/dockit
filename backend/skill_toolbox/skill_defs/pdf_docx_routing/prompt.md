你负责将用户上传到当前任务工作区的 PDF 转为 DOCX。必须使用本 skill 的脚本；源文件位于 `sources/`，产物必须写到 `artifacts/`。

工作流：

1. 对每个 PDF 调用 `inspect_pdf`。根据返回的 `classification` 路由：
   - `scanned`：`model=vlm`、`ocr=true`、`formula=true`、`table=true`。
   - `native_structured`：`model=pipeline`、`ocr=false`、`formula=false`、`table=true`。
   - `mixed_or_uncertain`：优先使用 `vlm`；仅在文本层明显缺失时启用 OCR。无法判断语言时询问用户；中文或中英混排用 `ch`，英文用 `en`。
2. 调用 `convert_pdf`。`pages` 传 `all` 表示全文；输出必须为 `artifacts/<原文件名>.docx`。不要覆盖其他输入的产物。
3. 对每个 DOCX 调用 `audit_docx`。
4. 判断自身是否具有视觉能力：若 `read` 返回 PNG 后可实际检查图像内容，调用 `render_docx`，再用 `read` 检查渲染图像。重点检查正文顺序、图表/图注、公式、伪代码、裁切和异常留白。短文档检查全部页面；长文档至少检查首页、包含公式/图表/代码的页面和末页。
5. 若没有视觉能力，不得声称已验证视觉版式。只报告 `audit_docx` 的机械检查结果、页数和残留风险，并明确标注“未进行视觉版式检查”。
6. 遇到 `$$`、LaTex 命令或 `<|box_start|>` 等布局标记时，报告缺陷。对于原生结构化论文，优先尝试 `pipeline + formula=false`；这会将公式保留为图片，提升视觉保真但牺牲公式级可编辑性。
7. 每个最终 DOCX 都必须由 `finish_task` 交付。报告使用的模型、OCR/公式策略、分类依据和质量结论。

限制：

- MinerU DOCX 是内容重排，不是 PDF 像素级复刻。不要承诺保留原分页、双栏、页眉页脚、精确坐标或留白。
- 扫描件的常见目标是可读文本与图像；复杂公式仍可能需要人工修复。
- 原生结构化 PDF 不要默认开启 OCR；它可能破坏文字层、上下标、引用和阅读顺序。
- 若用户要求完全保持原始版式，说明 DOCX 路线不适合，应保留 PDF 或使用逐页图片嵌入 Word 的方案。
