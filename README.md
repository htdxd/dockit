# DocKit · AI 文档产物工具箱（skill-toolbox）

一个极轻量、即开即用的 AI 文档产物桌面工具。它将 Coding Agent 中经过验证的 Skill 能力封装为面向普通用户的小工具：选择一个日常任务、提供必要的材料与要求，软件在本地受控环境中调用用户自行配置的 LLM，最终输出可由专业软件（Word / WPS / PowerPoint）继续编辑的文件。产品交付的是「可用产物」，而不是通用 AI 对话能力。

## V1 工具

| 工具 | 输入 | 输出 |
|---|---|---|
| PPT 生成 | 主题、材料、风格、页数 | `.pptx` |
| 简历生成 | 经历、目标岗位、材料 | `.docx` / `.pdf` |
| DOCX 生成 | 结构化要求、参考材料、复杂度档位 | `.docx` |
| PDF 转 DOCX | PDF 文件 | 可编辑 `.docx` |

## 架构

```
src/                 Tauri v2 + Vite + TypeScript 前端（无 UI 框架）
backend/             Python skill-toolbox sidecar（AgentRuntime + Skill 运行时）
  skill_toolbox/
    runtime.py       极简 Agent tool-call loop（受控 workspace、超时、可审计日志）
    capabilities.py  模型能力解析（vision 静态表 + 用户覆盖）
    skill_defs/      Skill 清单：docx_pro / pdf_docx_routing / ppt-master（上游单独跟踪）
tests/               pytest（运行时、能力路由、docx_pro 端到端）
src-tauri/           Rust 宿主：spawn sidecar、stdin/stdout JSON-lines 协议
```

### 能力感知路由

- Skill manifest 声明 `required_capabilities` / `optional_capabilities`（如 `tool_calling`、`json_schema`、`vision`）。
- 任务开始前解析模型能力：用户显式覆盖 > 静态模型表 > 安全默认；缺失必需能力时**明确报错，不静默降级**。
- 运行时把 `[CAPABILITIES]` 横幅注入 system prompt，Skill 依据横幅路由（如 DOCX 的视觉版式校验 vs 机械质量门）。

### docx_pro 生成流程

`build_docx`（封面配方 / CJK 版式 / WPS 兼容 / OMML 公式）→ `inject_toc`（TOC 域 + updateFields）→ `postcheck_docx`（15 条业务质量门，含表格跨页、CJK 缩进、占位符泄漏）→（vision 模型）`render_pages` 渲染 PNG 逐页核验。复杂档位支持填报表单（SDT + 文档保护）、公文 GB/T 9704、模板套用，OfficeCLI 可用时可委托原生图表/水印/邮件合并。

## 开发

```bash
# 前端
npm install && npm run dev        # vite dev (127.0.0.1:1420)
npm run build && npm test

# 后端（uv base 环境）
uv run pytest tests/ -q

# 桌面端（需 Rust toolchain）
npm run tauri dev
```

## 模型接入

OpenAI / Anthropic / OpenAI-compatible 网关均可：在设置页填写 `base_url`、`api_key`、模型名，任务开始前校验能力。PDF 解析等第三方服务（MinerU）的 Token 在设置页「第三方服务」中配置。

## 许可证

内部项目，未发布。第三方参考：MiniMax minimax-docx（MIT，规则参考）、iOfficeAI OfficeCLI（Apache 2.0，可选集成）。
