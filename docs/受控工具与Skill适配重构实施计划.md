# 受控工具与 Skill 适配重构实施计划

## 1. 文档状态与执行规则

- 状态：规划草案，等待用户明确确认“计划完成”。确认后本文冻结，执行阶段视为只读。
- 目标：简化 PPT、简历、DOCX、PDF 转 DOCX 四个功能的 Agent 工作流，统一 Prompt、LLM tool schema、后端校验和底层脚本行为。
- 当前工作区可能包含用户已有的未提交修改。实现者必须先运行 `git status --short`、`git log -1 --oneline`，记录基线；不得回滚、覆盖或清理未由自己创建的改动。
- 本计划不启用多 Agent。除非用户另行授权，不得委派子任务或并行修改同一代码边界。
- 实施必须按阶段顺序进行。每阶段通过自己的测试门和验收条件后才能进入下一阶段；不得让四个 Skill 同时处于不可运行状态。
- 实现者使用无 Vision LLM 时，必须遵守本文第 8 节的无 Vision 规则。依赖视觉判断的步骤可以显式跳过，但必须留下结构化 `skipped/not_run` 状态、原因和待 review 项，不得把“无法查看图片”伪装成视觉检查通过。
- Python 命令使用 `uv run`；安装依赖使用 `uv pip install`；测试使用现有项目虚拟环境和 `pytest`。

## 2. 问题与目标

### 2.1 已观察问题

现有系统已经具备 `DocumentIR`、`ContentPlan`、Skill manifest、质量门和任务日志，但 LLM 仍直接面对过于底层的操作：

- `read`、`write`、`edit`、`exec_cmd`、`spec_append` 让模型管理路径、JSON、脚本 action 和中间状态；
- Prompt、manifest 和后端校验存在重复契约，容易出现错误 action、错误路径、错误 JSON 和无效重试；
- DOCX 生成需要模型自行维护 `spec.json`，简历需要模型自行维护 `resume_data.json`；
- 简历组件动作虽然已有 manifest 白名单，但仍通过数据文件和通用脚本间接操作；
- `MaterialService` 已实现任务内同 hash 复用，但 PDF 的 MinerU 结果尚未形成跨任务持久缓存；
- `ppt-master` 包含大量通用上游能力，本项目 V1 只需要其中一小部分，不能把其完整自由工作流暴露给普通任务；
- 日志中已经出现 ContentPlan 缺失、图片路径自造、`old_text` 不匹配、JSON 损坏、block 超限、错误 action、渲染产物路径错误和达到步数上限等模式。

### 2.2 目标

将系统改造成：

```text
LLM 只做内容和受限决策
  → 领域级 LLM tools
  → 内部 typed Python Service
  → vendor/Skill 脚本或确定性库
  → 机械质量门
  → 无 Vision 保守交付 / Vision 有界复核
```

成功路径的目标步数（用于观测，不作为绕过质量门的硬编码）：

| 功能 | 目标正常步数 |
|---|---:|
| 简历 | 3-8 步 |
| DOCX | 4-10 步 |
| PDF 转 DOCX | 2-6 步 |
| PPT | 5-12 步 |

## 3. 总体架构

### 3.1 目录结构

目标结构如下，允许实现阶段根据真实依赖合并小模块，但不得重新把所有职责集中回一个大文件：

```text
backend/skill_toolbox/
  runtime.py                    # 任务生命周期、模型回合、发布门
  skills.py                     # Skill facade / manifest 加载
  models.py                     # Provider 与回合模型
  material_models.py            # DocumentIR、ContentPlan、QA 等契约

  contracts/
    __init__.py
    common.py                   # ArtifactRef、ToolError、OperationResult
    resume.py                   # ResumeRequest、ResumeChange、ResumeResult
    docx.py                     # DocumentDraft、DocxBlock、DocxResult
    ppt.py                      # DeckOutline、SlideSpec、PptResult
    pdf.py                      # PdfPrepareRequest、PdfResult

  tools/
    __init__.py
    legacy.py                   # 旧 ToolRegistry 的兼容实现，过渡期保留
    workspace.py                # 受控路径、文本/媒体读取、原子文件操作
    process.py                  # 受控 subprocess、超时、进程树回收
    cache.py                    # 通用 hash cache、原子提交、per-key lock
    materials.py                # read_material、content plan、Asset 解析
    mineru.py                   # MinerU adapter、持久缓存、版本/参数指纹
    resume.py                   # ResumeService、模板索引、组件动作和回滚
    docx.py                     # DocxService、草稿、生成、检查、渲染
    ppt.py                      # PptService、上游 ppt-master 适配
    pdf.py                      # PdfService、PDF 路由、审计、渲染
    qa.py                       # QAReport、质量 action 证据和状态聚合

  llm_tools/
    __init__.py
    common.py                   # tool schema 工厂和固定错误格式
    dispatcher.py               # LLM tool call → 内部 Service
    resume.py                   # 简历可见工具 schema
    docx.py                     # DOCX 可见工具 schema
    ppt.py                      # PPT 可见工具 schema
    pdf.py                      # PDF 可见工具 schema

  skill_defs/
    resume_pro/                 # 本项目 Skill facade、模板和兼容 CLI
    docx_pro/                   # 本项目 Skill facade、参考资料和兼容 CLI
    pdf_docx_routing/           # 本项目 PDF facade 和兼容 CLI
    ppt-master/                 # 本项目对上游 PPT Skill 的 facade

  vendor/
    ppt-master/                 # 外部仓库固定快照或 subtree，仅保存上游代码

  patches/
    ppt-master/                 # 无法在 adapter 层解决的最小补丁，按上游 commit 组织

  upstream.lock.json            # 上游仓库、ref、commit、版本、许可证和适配状态
```

### 3.2 分层职责

#### LLM-facing 层

`llm_tools/` 只负责 JSON Schema、参数解析、能力过滤、错误翻译和调用内部 Service。普通用户任务不得暴露 `write`、`edit`、`exec_cmd`、`spec_append` 或任意脚本 action。

#### 内部 tools 层

`tools/` 使用普通 Python 函数、class 和 typed request/result，不使用 LLM tool-call 数据结构。它负责：

- 相对路径解析和 workspace policy；
- JSON、DOCX、PPTX、PDF 的确定性处理；
- 脚本调用、超时、进程树清理；
- cache hit/miss、锁、原子提交和回滚；
- 质量报告和 artifact 版本。

#### Skill facade 层

`skill_defs/` 保留 Prompt、manifest、模板、参考资料和 CLI 入口。CLI 是兼容接口，不是新的业务边界。新 Runtime 优先调用 `tools/`，只有上游脚本无法安全 import 时才通过内部 process adapter 调用 CLI。

#### Vendor 层

本仓库当前将 `skill_defs/ppt-master/` 作为被 `.gitignore` 忽略的 vendor 快照目录；它只保存外部仓库代码，不直接承载本项目的材料 IR、缓存、Prompt、QA 或用户产品规则。若未来改为可追踪 subtree，必须同步更新 `upstream.lock.json` 与本节路径。

## 4. 上游 Skill 适配策略

### 4.1 四个 Skill 的归属

| Skill | 归属判断 | 处理策略 |
|---|---|---|
| `ppt-master` | 外部仓库，未来会持续更新 | vendor 固定快照/subtree + 本项目 `PptService` adapter；V1 只暴露生成、填充、验证、渲染所需能力 |
| `docx_pro` | 已被本项目深度改造的本地 facade | 脚本保留兼容入口；核心逻辑逐步由 `DocxService` 调用；不得用上游文件直接覆盖本地修改 |
| `resume_pro` | 本项目模板和组件动作实现 | 视为本项目自有代码；模板 manifest、组件索引、照片链路和布局规则不跟随外部 Skill 自动更新 |
| `pdf_docx_routing` | 本项目围绕 MinerU 的路由和审计 facade | `MineruService`/`PdfService` 负责缓存和任务契约；转换脚本仅作为兼容或 subprocess backend |

### 4.2 PPT vendor 更新

优先使用 `git subtree` 或可复现固定快照，不使用运行时在线拉取。每次更新必须：

1. 将上游仓库 URL、ref、commit、版本和许可证写入 `upstream.lock.json`；
2. 更新 `skill_defs/ppt-master`（或未来 lock 指定的 subtree 目录），不改写本项目 facade、Prompt 和 adapter；
3. 运行 `PptService` contract tests、SVG quality tests、PPTX package tests；
4. 对比上游 manifest、CLI 参数、输出目录、报告 JSON 和依赖变化；
5. 若契约变化，在 `tools/ppt.py` 适配，不要求 LLM 认识上游变化；
6. 只有无法通过 adapter 解决的上游缺陷才在 `patches/ppt-master/<commit>/` 留最小 patch；
7. 更新 `upstream.lock.json` 的 `adapter_status`、测试结果和人工复核结论；
8. 更新失败时保留旧 vendor commit，禁止把未通过测试的新版本直接用于 Release。

### 4.3 其他脚本同步

DOCX、简历和 PDF 当前存在大量本地修改。实现者必须先建立 `docs/` 或 `upstream.lock.json` 中的来源表：文件、来源、许可证、当前本地差异、是否可替换。未确认来源或许可证的文件不得直接从外部仓库覆盖。

## 5. 内部 Python API 与 LLM Tool 设计

### 5.1 通用内部结果

所有 Service 返回统一结构，至少包含：

```json
{
  "ok": true,
  "artifact_id": "artifact-...",
  "status": "created|validated|cache_hit|cache_miss|failed",
  "warnings": [],
  "issues": [],
  "next_action": null
}
```

失败结果必须包含稳定 `code`、人类可读 `message`、`retryable` 和唯一建议，不返回完整 traceback 给 LLM。

### 5.2 共享可见工具

四个 Skill 只共享以下工具：

| Tool | 参数 | 工作 |
|---|---|---|
| `read_material` | `source_id`、`view`、可选 `offset/limit` | 按 IR ID 返回摘要、正文分页或 Asset 元数据；不接受任意路径 |
| `create_content_plan` | `selected_source_ids[]`、可选 `excluded_source_ids[]` | 后端校验真实 ID，自动写入计划；未列出的 Asset 自动排除并留痕 |
| `ask_user_questions` | 结构化 `questions[]` | 一次批量澄清，限制轮数并记录答案 |
| `task_failed` | `code`、`message` | 明确终止，不伪造 artifact |

`finish_task` 由 Runtime 自动执行；如保留为 LLM 兜底，只接受已注册 `artifact_id[]`，不能接受任意路径。

### 5.3 简历可见工具

| Tool | 参数 | 工作 |
|---|---|---|
| `resume_prepare` | `template_id` | 校验模板 hash，返回字段/组件 schema、容量、白名单动作和缓存状态 |
| `resume_generate` | `template_id`、`fields`、可选 `photo_asset_id`、`target_role`、`formats[]` | 后端生成 resume data、填充候选副本、处理照片、自动溢出估算和机械检查 |
| `resume_repair` | `artifact_id`、`changes[]` | 仅允许 manifest 白名单内的字段替换、组件移动/扩高/复制；候选副本失败自动回滚 |

`changes[]` 只允许 `component_id`、`text`、`move_rows`、`resize_rows`、`asset_id` 等有限字段，后端把行数转换为固定几何动作。

### 5.4 DOCX 可见工具

| Tool | 参数 | 工作 |
|---|---|---|
| `docx_start` | `title`、`complexity`、可选 `scene/author/date` | 创建内部草稿，不要求模型写 `work/spec.json` |
| `docx_add_blocks` | `document_id`、`blocks[]` | 追加有序 heading/paragraph/image/table/formula；图片只接受 `asset_id` |
| `docx_finalize` | `document_id`、`output_name` | 自动生成、目录注入、机械检查和 artifact 注册 |
| `docx_repair` | `artifact_id`、`issue_id`、结构化修复参数 | 只修复质量报告点名的问题 |

内部可继续调用 `spec_append`、`build_docx`、`inject_toc`、`postcheck_docx` 和 `render_pages`，但这些不再对 LLM 暴露。

### 5.5 PPT 可见工具

| Tool | 参数 | 工作 |
|---|---|---|
| `ppt_create_outline` | `topic`、`audience`、`page_count`、`style`、`selected_source_ids[]` | 生成有界页面大纲和布局类型 |
| `ppt_generate` | `outline_id`、`slides[]`、可选 `template_id` | 后端调用 vendor adapter 生成 PPTX；禁止模型写任意 SVG 项目文件 |
| `ppt_repair` | `artifact_id`、`issues[]`、有限文本/布局修改 | 只修复质量报告指定的问题 |

内部可调用 `source_to_md`、`svg_quality_check`、`svg_to_pptx`、`fill_validate` 和渲染脚本。`project_manager`、`project_import` 等上游通用 action 不对 V1 LLM 暴露。

### 5.6 PDF 可见工具

| Tool | 参数 | 工作 |
|---|---|---|
| `pdf_prepare` | `source_id`、`priority`、可选 `pages/language` | 查缓存；未命中时调用 MinerU；注册准备 DOCX 和 IR |
| `pdf_audit` | `document_id` | 执行类型/结构/关系/残留标记审计 |
| `pdf_repair` | `document_id`、`issue_id`、有限 `fix` | 只执行表格、公式、图片尺寸和顺序等明确修复 |
| `pdf_finalize` | `document_id` | 写 QA、注册 artifact 并交付 |

## 6. MinerU 持久缓存

### 6.1 Cache key

缓存键必须至少包含：

```text
pdf_sha256
mineru_adapter_version
mineru_cli_or_api_version
convert_options_hash
language
model/mode
```

同一 PDF 只要转换选项或解析器版本变化，就视为不同缓存项。

### 6.2 缓存布局

```text
<app-data>/skill-toolbox-cache/mineru/<cache_key>/
  result.docx
  result.json
  metadata.json
  COMPLETE
```

测试可通过配置将根目录指向 `output/.skill-toolbox-cache/mineru/`。缓存不得放进临时任务 workspace，也不得写入 API Token、Authorization header 或用户隐私以外的调试原文。

### 6.3 命中与未命中规则

1. 计算源 PDF hash 和选项 hash；
2. 获取 per-key 文件锁；
3. 检查 `COMPLETE`、metadata 版本、结果文件存在性和 DOCX 可读性；
4. 完整命中时复制/链接结果到本任务 workspace，返回 `cache_hit`，禁止调用 MinerU；
5. 未命中时返回 `cache_miss`，调用 MinerU；
6. 写入临时目录并完成审计后，以原子 rename 提交缓存；
7. 失败只清理临时目录，不生成 `COMPLETE`；
8. 缓存损坏或版本不兼容按 miss 处理，不覆盖其他有效 cache key。

### 6.4 测试门

必须覆盖：首次 miss 调用一次 API、同 hash hit 不调用 API、选项变化 miss、版本变化 miss、损坏缓存 miss、并发同 key 只调用一次、取消/超时无半成品、Token 不落日志。

## 7. 四个功能的目标工作流

### 7.1 共享流程

```text
Runtime 初始化任务
→ MaterialService 计算 hash 并解析/复用 IR
→ read_material 读取必要摘要
→ create_content_plan 记录选择
→ 领域生成 Tool
→ 内部机械质量门
→ 无 Vision：完成机械门，视觉步骤显式跳过并登记待办；Vision：渲染后再做视觉门
→ 必要时最多 3 次领域 repair
→ Runtime 自动注册并交付 artifact
```

### 7.2 简历

正常路径：`resume_prepare → read_material → create_content_plan → resume_generate → resume_repair(可选) → 自动交付`。

约束：模板预处理一次完成；模板源只读；字段、照片和组件动作必须来自 manifest；不得让模型直接编辑 DOCX/XML；无 Vision 只使用高置信度照片和机械检查；依赖视觉的照片确认、渲染判断可以跳过，并在 QA/报告中登记待 review；`move_rows/resize_rows` 由后端转换为安全 pt 并检查页面边界和重叠。

### 7.3 DOCX

正常路径：`read_material → create_content_plan → docx_start → docx_add_blocks(多次) → docx_finalize → docx_repair(可选) → 自动交付`。

约束：图片只传 `asset_id`；block 结构由 Pydantic/typed contract 校验；模型不写 `spec.json`；机械门必须成功；无 Vision 不得读取渲染图或声称视觉通过；视觉复核可以跳过并生成待 review 记录。

### 7.4 PDF 转 DOCX

正常路径：`pdf_prepare → pdf_audit → pdf_repair(可选) → pdf_finalize → 自动交付`。

约束：`pdf_prepare` 是唯一 MinerU 入口；缓存命中不得访问 API；不重复转换原 PDF；无 Vision 只报告类型、页数、结构审计和残留风险；依赖截图的版式复核可以跳过并登记待 review；完全版式复刻不作承诺。

### 7.5 PPT

正常路径：`read_material → create_content_plan → ppt_create_outline → ppt_generate → ppt_repair(可选) → 自动交付`。

约束：V1 不让模型自由编辑 SVG；只允许固定布局、文本、图片和表格参数；vendor 脚本由 `PptService` 调用；机械结构门先于视觉门；无 Vision 使用保守布局、禁止自由裁剪和低置信度图片使用；视觉页检可以跳过并登记待 review。

## 8. 无 Vision LLM 实现特别要求

实现者必须按无 Vision 能力运行和测试。下列“视觉依赖步骤”允许跳过，但跳过不是通过：

- 所有任务使用 `capabilities.vision=false`；Provider 不得收到图片 payload；
- Prompt 不得要求模型读取 PNG 或判断视觉美观；
- `read_material` 只返回文本、caption、文件名、尺寸、aspect、bbox 和来源关系；
- 图片采用 `high_confidence_only`；不确定时一次批量询问或排除；
- 生成和 repair 工具不得要求模型输入坐标、EMU、XML、SVG 或路径；
- 视觉 QA 永远为 `not_run` 或 `skipped`，机械 QA 通过也不能改写为视觉通过；
- 允许跳过的步骤包括：候选图片内容确认、渲染 PNG 的语义/审美判断、PPT 页面视觉复核、简历文字重叠的视觉确认、PDF 转换后图表/公式的视觉确认；
- 每次跳过必须记录 `{step, reason, affected_artifacts, review_required: true}`，并在 `work/qa/pending-visual-review.json` 或等价结构化结果中落盘；
- 实现者不得因为没有 Vision 而跳过机械渲染/文件生成本身（只可以跳过对渲染结果的视觉理解）；
- 后续 review Agent 负责读取待办，补做视觉检查、修复或将其明确降级为“机械通过、视觉未验证”的交付状态；
- 机械规则必须覆盖文件完整性、关系完整性、路径存在性、组件白名单、容量/溢出、边界和新增重叠；
- 所有失败结果必须给出单一可执行下一步，禁止让模型在多个猜测方案之间循环。

无 Vision 实现者不得以“本地渲染成功”代替视觉验证。若实现者为了测试查看了本地截图，报告中必须说明这是人工/开发者检查，不是 LLM Vision 检查。实现者可以继续推进不依赖视觉的后续阶段，但必须在阶段报告中列出所有待 review 项。

## 9. 分阶段实施计划

### 阶段 0：基线和来源清单

目标：冻结当前行为、识别上游来源和本地差异。

工作：

- 记录 `git status`、当前 commit、Python/Node/Office/MinerU 版本；
- 为四个 Skill 建立来源表：仓库、ref、commit、许可证、当前本地修改、可否覆盖；
- 建立 `upstream.lock.json` 初版；
- 梳理现有测试与日志失败样本，按错误类别建立基线统计。

验收：来源不明文件被明确标记；当前测试基线可重复；没有删除任何用户改动。

### 阶段 1：内部 API 边界和兼容层

目标：建立 `contracts/`、`tools/`、`llm_tools/`，不改变现有功能行为。

工作：

- 将现有 `tools.py` 迁移为 `tools/legacy.py`，通过 `tools/__init__.py` 保持旧 import；
- 新增 workspace/process/cache 基础 API；
- 新增统一 `OperationResult`、`ToolError`、ArtifactRef；
- Runtime 暂时双路径运行：旧底层 tool 保持可用，新 Service 通过 focused tests 验证；
- 不在此阶段删除旧 `TOOL_SPECS`。

验收：现有后端测试通过；旧 `ToolRegistry` import 不回归；路径越界、超时和进程树回收测试通过；无新增通用权限。

### 阶段 2：MinerU 持久缓存

目标：实现跨任务可验证的命中/未命中缓存。

工作：

- 新增 `tools/mineru.py` 和通用 `tools/cache.py`；
- 将 `MaterialService._parse_native_pdf` 改为调用 `MineruService`；
- 支持 cache key、原子提交、COMPLETE 标记、per-key lock、缓存损坏恢复；
- 保持 Token 只在内存和受控子进程环境中传递。

验收：第 6.4 节全部测试通过；缓存 hit 日志明确且没有 MinerU 调用证据；失败和取消不产生可复用半成品。

### 阶段 3：共享材料和 ContentPlan 工具化

目标：让 LLM 不再读任意路径和手写 ContentPlan。

工作：

- 实现 `read_material`、`create_content_plan` 内部 Service 和 LLM adapter；
- ContentPlan 由后端写入并补齐 task_type/mode/schema_version；
- Asset 未列出时自动排除并留痕，保留显式排除原因；
- 逐步把四个 Prompt 改为只描述领域工具，不再要求 `write` 计划文件。

验收：真实 ID、重复、未知 ID、Asset 遗漏、vision=false 模式均有测试；模型不需要写任何计划 JSON。

### 阶段 4：简历领域 Service 和模板预处理

目标：先解决简历替换失败、任意文本不可编辑和布局越界问题。

工作：

- 为 5 套模板生成/校验稳定组件索引；
- 将 `fill_resume.py` 核心替换、照片、容量估算、组件动作提取到 `tools/resume.py`；
- 运行时只暴露 `resume_prepare`、`resume_generate`、`resume_repair`；
- 所有动作在候选副本执行，失败自动丢弃；
- 将字段长度、组件容量、上下移动行数和 resize 行数纳入 typed contract。

验收：5 套模板 hash 和字段索引测试通过；未索引字段明确返回 unsupported；照片替换/删除、标签去重、溢出、边界、重叠和回滚测试通过；无 Vision 不读取图片 payload；视觉相关确认可以登记为 pending review，不得伪造通过。

### 阶段 5：DOCX 领域 Service

目标：隐藏 `spec_append/write/edit/exec_cmd`，降低大 JSON 和图片路径错误。

工作：

- 实现 `docx_start`、`docx_add_blocks`、`docx_finalize`、`docx_repair`；
- 内部通过 asset_id 解析图片，不接受任意图片路径；
- 将 build、TOC、postcheck、render 编排为后端操作；
- 保留现有脚本 CLI 作为兼容入口并增加 adapter contract tests。

验收：顺序 block、图片、表格、公式、TOC、质量门测试通过；错误 block 只返回一个明确修复建议；无 Vision 视觉状态为 not_run/skipped，并有待 review 清单。

### 阶段 6：PDF/MinerU 领域 Service

目标：让 PDF 转 DOCX 只发生一次转换，并彻底隐藏 MinerU 参数。

工作：

- 实现 `pdf_prepare`、`pdf_audit`、`pdf_repair`、`pdf_finalize`；
- `pdf_prepare` 统一 inspect、缓存查找、MinerU 转换、准备 DOCX 注册；
- 删除 Prompt 中要求 LLM 自行调用 convert/inspect 的底层步骤；
- 将审计、渲染结果转换为结构化 issue。

验收：缓存命中不调用 MinerU；未命中成功后进入缓存；审计失败不允许交付；多文档渲染前缀隔离；无 Vision 不读取 PNG 内容，视觉复核可登记 pending review。

### 阶段 7：PPT vendor adapter

目标：保留上游 PPT 能力，同时限制 V1 的自由度和升级风险。

工作：

- 固定 `vendor/ppt-master` 上游版本；
- 实现 `tools/ppt.py`，封装 project init、outline、固定布局生成、quality check、导出；
- 不把 `project_manager`、`project_import`、自由 SVG authoring 和非 V1 能力暴露给 LLM；
- 建立上游升级 smoke/contract tests；
- 将上游生成失败转换成稳定 issue，不让模型猜命令行参数。

当前实现状态约束：`PptService` V1 已完成固定 `python-pptx` 生成、结构门、vendor 指纹和可选 `pptx_delivery_check`；上游 `ppt-master` 的 SVG 生成器尚未接入生成路径，`upstream.lock.json.adapter_status` 必须明确标注这一点。接入前不得声称“已复用 ppt-master 生成”。

验收：固定输入可生成可编辑 PPTX；结构门失败不会交付；vendor 版本变更能被测试发现；无 Vision 使用保守图片策略；视觉页面复核可跳过但必须进入待 review 清单。

### 阶段 8：Runtime、Prompt 和 manifest 切换

目标：将四个 Skill 切换到领域工具 schema。

工作：

- Runtime 根据 Skill 返回不同的 LLM tool schema；
- 逐个删除 Prompt 中的底层 `exec_cmd` 说明；
- finish、QA、质量 action 证据由 Runtime/Service 自动维护；
- 保留 Developer/兼容模式的旧工具，但普通任务默认不可见；
- 统计每个任务步数、工具失败类别和 cache hit rate。

验收：四个 Skill 端到端测试通过；普通模式不会出现底层工具名；工具参数校验与后端行为一致；目标步数达到第 2 节范围。

### 阶段 9：全量回归与发布门

目标：验证旧功能未回归、升级路径可操作、无 Vision 结果诚实。

工作：

- 运行后端完整 pytest、前端测试、构建和类型检查；
- 运行无 Vision 端到端 fixture；
- 运行缓存、并发、取消、超时、路径越界、日志脱敏检查；
- 对四类历史日志失败模式各建立至少一个回归测试；
- 生成实现报告和已知风险清单。

验收：没有未解释的失败测试；没有 API Key/Token/raw CoT 泄漏；未通过视觉检查的产物明确标记 not_run/skipped 并带待 review 项；发布包包含 vendor lock 和适配版本。

## 10. 上游更新操作规程

以 `ppt-master` 为主要示例：

1. 在独立更新分支拉取上游仓库；
2. 只更新 `skill_defs/ppt-master`（或 lock 指定的 subtree 目录）和 `upstream.lock.json`；
3. 禁止直接覆盖 `tools/ppt.py`、`llm_tools/ppt.py`、本项目 Prompt 和 contracts；
4. 运行 adapter contract tests、PPTX 结构测试、质量门测试和最小端到端任务；
5. 查看上游 manifest、依赖、CLI 参数和报告格式 diff；
6. 若只需参数映射或路径变化，修改 adapter；
7. 若必须修改 vendor，添加最小 patch 并记录原因；
8. 所有测试通过后才更新默认 lock commit；
9. 失败时保留旧版本，可随时回滚 vendor commit。

禁止：

- 在任务运行期间联网 `git pull`；
- 让用户任务自动安装上游依赖；
- 用“复制最新目录”覆盖有本地修改的 Skill facade；
- 未经过测试直接升级 PPT vendor；
- 把 vendor 内部路径、CLI 参数或上游工作流暴露给 LLM。

## 11. 无 Vision 实现者的强制交付报告

实现者完成每个阶段后，必须提供以下报告，不得只说“测试通过”：

1. 阶段名称、目标和实际完成项；
2. 当前基线 commit、vendor commit 和 `upstream.lock.json` 变化；
3. 新增、修改、删除的文件列表；
4. 每个文件的关键接口和大致变更行数；
5. LLM-facing tools 清单、每个参数 schema 和内部 Service 映射；
6. 旧工具到新工具的迁移表，以及仍保留的兼容入口；
7. MinerU cache hit/miss、版本变化、损坏缓存、并发锁和失败清理测试结果；
8. 执行过的测试命令及完整摘要：通过、失败、跳过和跳过原因；
9. 任务步数统计和工具失败类别统计；
10. 无 Vision 约束验证：图片 payload 数量、视觉 QA 状态、机械 QA 状态；
11. 不能验证的项目、未覆盖的边界和建议的后续检查；
12. 不得宣称“视觉布局已通过”。如果实现者人工查看了截图，必须注明“人工开发检查，不是 LLM Vision 验证”。

实现者可以在报告末尾追加“待视觉 Review 清单”，至少包含：artifact、页码或组件、疑似问题、影响、建议检查方式和是否阻塞交付。该清单是后续 review 的输入，不要求无 Vision 实现者自行解决。

最终阶段还必须报告：

- 四个功能各一条成功路径和至少一条失败路径；
- 每个历史日志错误模式是否有回归测试；
- PPT vendor 更新是否可重复；
- MinerU 缓存命中是否确实避免 API 调用；
- 是否保留旧 API 兼容性；
- 仍需我进行 review 的代码风险和疑点。

## 12. 非目标与禁止事项

- 本计划不实现通用文档编辑器、任意模板编辑器或任意 MCP 编排器；
- 不把无 Vision LLM 变成视觉模型；
- 不重写整个上游 `ppt-master`；
- 不在 vendor 中混入本项目业务规则；
- 不以复制/覆盖方式更新有本地修改的 DOCX、PDF、简历脚本；
- 不为减少步数而跳过 ContentPlan、机械质量门、QAReport 或 artifact 校验；
- 不把缓存命中当作质量检查通过，缓存只表示“转换结果可复用”；
- 不在生产代码中用 try/except 代替测试；
- 不生成或提交 API Key、MinerU Token、raw CoT、thinking block 或用户材料原文日志。

## 13. 最终验收标准

只有全部条件满足，才能将本计划标记完成：

1. 四个功能均使用领域级 LLM tools，普通模式不暴露底层通用脚本工具；
2. 领域工具参数与后端 contracts、Prompt、manifest 三者一致；
3. 旧 `ToolRegistry`/兼容 CLI 的迁移策略清晰，现有测试不无故回归；
4. 简历五套模板均通过 hash、字段、组件动作、溢出和回滚测试；
5. DOCX 顺序 blocks、图片 asset_id、表格、公式、TOC 和质量门通过测试；
6. PDF MinerU cache 命中不调用 API，未命中成功后进入缓存，损坏/过期缓存可恢复；
7. PPT vendor 版本、适配层、patch 和 contract tests 可追溯；
8. 无 Vision 任务不发送图片 payload，不执行视觉断言，QA visual 为 `not_run/skipped`，并存在待 review 清单；
9. 机械失败、路径越界、关系损坏、溢出、重叠和不支持动作均不能交付；
10. 真实任务正常路径步数明显低于当前 24/60 步上限，并有日志证据；
11. 完整测试、构建、类型检查和无 Vision 端到端 fixture 结果均已报告；
12. 实现者已提交第 11 节要求的最终报告，明确列出剩余风险，等待主 Agent review。
