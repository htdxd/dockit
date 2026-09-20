# 第三方来源与分发边界

当前包含 PyMuPDF/MuPDF 的完整程序按 AGPL-3.0-only 发布并提供源码。该许可不替代下列模板、依赖、字体、图片或外部服务各自的许可。此文件是来源清单，不是完成法律审查的声明。

## 简历模板

`backend/skill_toolbox/skill_defs/resume_pro/templates/` 的原件来自 [mmmlllnnn/ResumeCollection](https://github.com/mmmlllnnn/ResumeCollection)，上游 MIT，Copyright (c) 2024 mln。许可全文见 [模板目录 LICENSE](backend/skill_toolbox/skill_defs/resume_pro/templates/LICENSE)。发行包仅包含已组件化的 t001、t109。

t001、t109 的上游示例照片已移除，替换为本项目 `scripts/sanitize_release_templates.py` 绘制的中性占位图；预览图由替换后的 Word 原件重新渲染。未开放的模板不再分发。模板其余版式与图形沿用上游 MIT 许可及声明。

## 运行依赖

- Microsoft Word：专有软件，用户/部署方自行安装和取得许可，发行包不包含 Word 或字体。WPS 的兼容 COM 不视为支持。无人值守服务的稳定性与许可需单独评估。
- PyMuPDF 1.28.2 / MuPDF 1.28.2：使用 AGPL-3.0 开源许可。Release 附对应源码归档，代码仓库、桌面/网站页脚均提供源代码入口。[官方仓库](https://github.com/pymupdf/PyMuPDF)。
- Poppler 26.07.0：GPL；使用未修改的独立程序。Release 附对应源码，`licenses/` 保留上游 COPYING 及第三方声明。动态运行库保持各自许可。
- MinerU Open API CLI 0.5.9：Apache-2.0，许可保存在 `licenses/MinerU-Ecosystem-LICENSE`。[CLI 源码](https://github.com/opendatalab/MinerU-Ecosystem/tree/main/cli/mineru-open-api)。这是云 API 客户端，不是本地 MinerU 大模型；云服务另适用供应商条款。
- Python、OpenAI/Anthropic SDK、python-docx、Pillow、pywin32、Tauri 等：遵循各自发行物许可；构建输出 `python-packages.json` 记录具体版本，运行包保留包内许可文件。

## 退役资源

PPT 制作与通用 Word 制作不属于当前产品。原本地 `ppt-master` 外部快照不再加载或打包，也不随源码仓库分发。历史架构文档只用于追溯，不代表当前功能。

## 获取对应源码

本项目源代码与构建脚本：https://github.com/htdxd/dockit 。Release 同时提供源码快照和 `DocKit-Resume-0.2.0-PDF-Sources.zip`（PyMuPDF、MuPDF、Poppler）。精确下载地址和 SHA-256 在 `licenses/sources.json`。运行 `uv run scripts/release_sources.py` 可重新取得这些上游源码及许可；其他 Python/Rust/npm 依赖按 uv.lock、Cargo.lock、package-lock.json 获取，发行包保留各包许可文件。
