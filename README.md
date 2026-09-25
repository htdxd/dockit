# DocKit Resume

面向中文简历的 AI 制作工具：从零整理经历、优化已有简历、迁移到组件化模板，并用自然语言修改内容与样式。输出可编辑 DOCX 或 PDF。

[下载 Windows 版与源码](https://github.com/htdxd/dockit/releases/latest)

## 当前能力

- 六套经过实际排版验证的中文模板：t001、t002、t003、t015、t024、t109，共用编辑和排版流程。
- 上传 PDF、DOCX、TXT、Markdown、PNG/JPEG/WebP，或手动填写资料，两者可同时使用。
- 首次生成前准备全部解析文字；视觉模型逐张转录上传图片，保留来源与无法辨认项。原图与机械提取工具用于按需复核。
- 通过问答补齐项目/实习的关键事实；GitHub、作品集、Stars/Forks 等由用户按需提供，不联网抓取或编造。
- 自定义个人字段、栏目与项目指标；支持字号、整体缩放、链接，以及字段/正文短语的加粗、主题色和浅底纹。
- 项目首行按“名称 · 用户名/项目名 · Stars/Forks · 项目性质”紧凑排布，技术栈独立下一行；项目日期保留数据但不显示，教育/工作日期正常显示。
- Word/WPS 实际测量文本高度与换行，布局后检查边界、重叠、文字完整性，再向视觉模型返回页面进行复核。
- 桌面版与简历专用网页版。PPT 制作、通用 Word 制作、独立 PDF→DOCX 转换已退出产品。

## 环境要求

高质量生成路径需要 **Windows + Microsoft Word 或 WPS 文字 + Poppler**。两者都安装时优先使用 Word；设置环境变量 `DOCKIT_OFFICE_ENGINE=word` 或 `wps` 可强制指定。测量与 PDF 导出始终使用同一个程序，生成记录的 `renderer` 字段写明实际导出程序。本机验证使用 Word 16.x（Office 2021）与 WPS 12.1；两者对同一段落的换行绝大多数一致，个别段落可能相差一行，因此同一份简历在两个引擎下的分页不保证完全相同。生成的 DOCX 可由兼容软件打开，但不同软件显示可能有差异。

需要配置支持工具调用的 LLM。图片内容理解与视觉验收需要视觉能力；未启用视觉时会明确标记未识别图片与未执行的视觉检查。PDF/DOCX 主解析使用 MinerU 云服务，需要 CLI 与 Token。

这不是跨平台、无需 Office 的纯浏览器编辑器。当前没有统计意义上的首稿成功率承诺。

## 开发运行

```powershell
uv sync --extra web
npm ci
npm run tauri dev
```

模型与 MinerU 在桌面设置页配置。用户资料、密钥、日志、数据库和 output/outputs 不应提交到 Git。内部 skill_toolbox 包名、应用 ID 与旧数据目录保留兼容，展示名称为 DocKit Resume。

```powershell
uv run --extra web pytest
npm test
npm run build
```

Word/WPS 端到端测试需要本机安装环境，可用 `DOCKIT_OFFICE_ENGINE` 分别运行；纯逻辑测试不会调用付费模型。真实模型效果仍需单独验证。

## 部署与打包

- 桌面安装包：先 `uv run scripts/release_sources.py`，再 `npm run package`。
- Windows 网页版：`uv run --extra web scripts/package_web.py`；按 [部署说明](deploy/resume-web/DEPLOY.md) 配置服务器密钥。
- 环境检查：发行目录运行 `start-web.bat --check`，验证 Word/WPS 身份、代表模板（t001/t109）的实际测量、PDF 导出和页面图片。
- [压测说明](deploy/resume-web/LOADTEST.md)：匿名无限制模式仅用于本机小范围压测，不用于公开访问。

不要把模型/MinerU 密钥放入前端或公开安装包。Microsoft Office 无人值守服务的可靠性与许可需要独立评估。

## 数据处理

桌面版文件与日志保存在本地，但所需材料会发送到配置的模型与 MinerU 服务；不是“数据从不离开本机”。网页版还会将上传材料、产物和任务日志保存到部署服务器。请只上传本次制作需要的信息，并了解供应商的处理政策。

## 代码结构

- `src/`：共享桌面/网页简历界面与控制器。
- `src/providerConfig.ts`、`providerRequests.ts`、`settings.ts`：配置规则、请求状态与持久化分别维护。
- `materials.py`、`parsers/`、`material_assets.py`：材料登记与缓存、格式解析、资源路径与复制分别维护，共用 `ProcessRunner`。
- `backend/skill_toolbox/material_intake.py`：生成前的图片转录与材料上下文准备。
- `runtime.py`、`agent_loop.py`：任务入口与模型工具循环；只有当前任务开放的工具才能执行。
- `resume_task.py`：材料准备、问答、工具绑定与交付装配；无需通用文件读写或脚本执行工具。
- `contracts/resume_workflow.py`、`tools/resume_workflow.py`：简历语义操作，schema 与后端校验同源。
- `resume_actions.py`：一次计算内容编辑；`tools/resume_store.py` 管理版本、接受状态和预览记录。
- `resume_layout/`：六模板的 SPEC 与间距档案，以及共享的 Word 测量、布局、DOCX 输出、渲染 QA。
- `providers/`：官方 SDK 协议适配；可重试的模型请求最多重试 3 次（含首次最多 4 次）。
- `web/`、`deploy/resume-web/`：网页版服务、环境诊断与压力测试。
- `docs/`：设计与研究；旧日期文档可能描述已经退役的功能，以当前源码与本 README 为准。

v0.3.0 已退役 legacy 通用工具模式和旧版简历工具入口。组件化模板沿用 `resume_prepare/generate/edit/preview/accept`，已接受版本的格式、hash、页数和质量检查继续作为交付条件。

## 开源与第三方许可

项目采用 [AGPL-3.0](LICENSE)，完整代码与构建脚本公开；若修改后以网络服务提供功能，也应按许可向使用者提供对应源码。模板与运行依赖见 [第三方说明](THIRD_PARTY_NOTICES.md)。

PyMuPDF/MuPDF 按 AGPL 开源许可使用，Release 附对应 PDF 引擎源码包。各模板的示例照片已换成自绘占位图；上游模板原有 MIT 声明继续保留。

本轮同类项目比较与后续优先级见 [开源定位与优化报告](docs/开源定位与优化报告-2026-09-20.md)。
