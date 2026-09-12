# PDF 转 DOCX 功能下线实施计划

日期：2026-09-11。状态：独立计划草案，待用户确认实施；本轮只创建计划，不删除功能或数据。
本计划独立于《简历组件化排版实施计划》，不启动新的 Codex 任务或委派 agent。

## 1. 目标与边界

下线面向用户的独立 PDF 转 DOCX 功能，移除导航、新建转换任务、模型转换工具及专用执行链。停止承诺版式还原或通用格式转换质量，把维护重点放在材料组织、文档生成与简历排版。

必须保留：PDF 作为简历、DOCX、PPT 的输入材料；文本/图片/表格等现有解析能力；PDF 预览及已有导出能力；用户历史任务、原始材料、转换产物和缓存。此次不接第三方网盘、不换解析供应商、不优化转换质量、不修改简历组件引擎。

“下线功能”不等于移除所有 PDF→DOCX 技术步骤：当前共享材料解析使用 DOCX 作为中间表示。本次保留这个内部步骤，不展示为独立转换服务，也不把中间 DOCX 自动发布成用户转换产物。未来若改为 PDF→DocumentIR 直连，另立计划，不作为本次完成的前提。

## 2. 已核实的依赖与风险

| 当前位置（相对项目根） | 事实与处理方向 |
| --- | --- |
| `src/ui.ts` | 存在 PDF 转 DOCX 导航及 `page-pdf` 新建页；移除新建入口，安排历史结果可达路径 |
| `src/main.ts` | `SKILLS.pdf`、转换 prompt、materialsByTool.pdf、产物分桶相互关联；分别处理，不能全局删除 pdf 分支 |
| `backend/skill_toolbox/materials.py` | `_parse_native_pdf` 直接定位 `skill_defs/pdf_docx_routing/scripts/convert_pdf.py`；经 MineruService 生成中间 DOCX，再复用 `_parse_native_docx` 产出 IR |
| `backend/skill_toolbox/tools/mineru.py` | 共享 MineruService、持久缓存、错误与 Token 注入契约；保留 |
| `backend/skill_toolbox/tools/pdf.py`、`contracts/pdf.py`、`llm_tools/pdf.py` | 独立转换领域 Service、请求模型及工具定义；依赖清理后删除 |
| `backend/skill_toolbox/runtime.py`、`llm_tools/dispatcher.py` | 注册 PdfService、pdf 工具、超时/交付动作与任务类型；移除活动路由，保留共享图像、QA 和产物基础设施 |
| `backend/skill_toolbox/skill_defs/pdf_docx_routing/` | 包含 manifest、prompt、convert/inspect/audit/render 脚本；先拆共享依赖，后移除专用 skill |
| `backend/skill_toolbox/sidecar.py` | 新建任务加载 skill；旧文件名 `.pdf_docx_routing.` 用于历史产物归类，不能随入口一起无条件删掉 |
| `backend/skill_toolbox/material_models.py` | TaskType 包含 `pdf_to_docx`；新建任务禁止，历史反序列化是否仍需此值须核实 |
| `.codex/skills/pdf-docx-routing/` | 存在项目级开发技能及元数据；实施时核对是否仅为已下线功能服务，再处理其失效引用 |

当前工作区有大量未提交修改，且简历 P2 涉及 Runtime、dispatcher、contracts 等共享文件。实施前记录接手差异，只作局部编辑；不得 reset/clean、覆盖其他 agent 的修改或同时让两名实现者编辑相同文件。原始数据目录不在删除范围。

## 3. 具体实施顺序

### D0：冻结范围与建立基线

检查项目 AGENTS.md 和 personal-engineering，定位所有活动调用与历史兼容读取。记录本次允许修改/删除/迁移的文件清单、现有测试结果及预存失败。检查是否有正在运行的转换任务；上线切换前使其完成或采用正常取消流程，不在执行中删除脚本，不强杀用户进程。

测试是本次下线的必要实施项；计划阶段不修改测试或运行业务测试。初始重点参考 `test_phase6_pdf_service.py`、`test_phase4_docx_pdf.py`、`test_phase1_reliability.py`、`test_phase3_runtime.py`、`test_sidecar.py`、`test_mineru_cache.py` 和材料相关测试，随后按实际调用范围补充。

### D1：先迁出共享 PDF 材料解析适配器

把实际被材料解析使用的 `convert_pdf.py` 迁到不依赖 skill 的内部路径，例如 `backend/skill_toolbox/parsers/mineru_pdf.py`，必要的同目录辅助依赖随迁；实际文件名按仓库现有约定确定。保持 CLI 参数、结果结构、超时、取消、错误码和 Token 隔离不变。不要同时重写解析实现。

更新 `MaterialService._parse_native_pdf` 的脚本定位与对应测试。保持 `source_format=pdf`、原始材料引用、图片资源关系、`prepared_docx` 和现有 manifest 读取兼容。中间 DOCX 继续由材料目录管理，不注册为可下载的“转换结果”。MineruService 不再通过转换 skill 目录寻找资源。

确认打包构建会包含迁移后的脚本，不能只在源码工作区可运行。若只改路径而行为不变，保留原有缓存身份；若路径/脚本指纹实际进入缓存 key，须验证并提供兼容读取或版本失效方案，禁止直接删除全局缓存。命中缓存不访问转换服务，未命中仍走既有认证与解析流程。

检查 `inspect_pdf.py`、`audit_docx.py`、`render_docx.py` 的真实调用者；共享用途先迁到适当公共层，纯转换用途才删除。不得因文件位于下线目录就认定可删。D1 必须先证明 PDF→材料 IR 仍工作，才进入 D2。

### D2：关闭产品入口与后台新建能力

从 `src/ui.ts` 移除转换导航、新建表单、专用上传和提交按钮；从 `src/main.ts` 移除对应 SKILLS 映射、新建 prompt 和无调用状态。保留其他功能的 PDF 上传、格式选项、预览、图标及扩展名识别。持久化的当前页面若为 pdf，新版应安全跳转到有效默认页，不能留下空屏或 JS 异常。

在 Sidecar 新建任务入口及 Runtime 直接调用边界统一拒绝 `skill_id=pdf_docx_routing` / `task_type=pdf_to_docx` 等旧请求，返回稳定的 `FEATURE_RETIRED` 错误和“PDF 转 DOCX 功能已下线；PDF 仍可作为生成材料”的提示。错误在调用模型、认证/Token 检查和材料预处理之前返回，不应产生新任务产物或远端计费。旧任务的查看不触发此拒绝。

移除模型可见的 `pdf_prepare`、`pdf_audit`、`pdf_repair`、`pdf_finalize`，以及关联 dispatcher handler、DomainServices.pdf、Runtime service 初始化、工具超时、交付动作和任务类型活动映射。旧工具调用明确拒绝或返回工具不可用，不重定向到其他生成技能，不保留一条可绕过 UI 的转换后门。

更新 skill 加载/发现，使下线 skill 不再作为可运行能力列出。可保留轻量 retired-id 兼容表用于明确报错和历史识别；兼容表不得加载转换执行代码。

### D3：保留历史任务与成果访问

旧任务记录、原始文件和已完成 DOCX/PDF 一律原位保留，不改名、不搬移、不清空。旧任务页可以打开或下载产物，显示“该功能已下线”；重试/新建转换入口禁用。失败或取消的旧任务仍可查看状态，不能伪装成成功任务。

推荐把旧转换结果接入现有文档结果列表，保留“历史 PDF 转 DOCX”来源标签；不新增可执行的转换页面。调整 Sidecar 的 `.pdf_docx_routing.` 来源标记与前端 `bySkill.pdf` 消费方式，使旧成果仍可达。服务端兼容保留 pdf bucket 可以接受，但活动工具列表不能因此重新出现转换能力。

无来源标记的历史 PDF 仍使用通用成果兜底展示；简历导出的 PDF 归简历，不误归历史转换。必须验证“页面被删但结果仍只落进隐藏 bucket”的情况。若当前 UI 没有合适的通用结果入口，先提供具体复用方案供审阅，再移除旧结果视图。

历史 schema 字面量按读取兼容需要保留，新请求使用独立的活动类型校验，不为删除一个枚举值破坏旧 JSON/数据库记录。无需全库迁移；若实测必须迁移，先提供备份与可逆方案另行确认。

### D4：清理专用文件、依赖与产品说明

D1–D3 验证后删除无共享用途的转换 skill manifest/prompt/脚本，以及 `tools/pdf.py`、`contracts/pdf.py`、`llm_tools/pdf.py` 等专用实现和导出。逐项清理 Runtime、dispatcher、tool_specs、skills 及来源索引引用，确认模块 import 不再依赖被删文件。

更新 README 的四功能描述、功能表和源码目录说明；当前产品范围文档如仍承诺转换则修正。历史计划与评审证据保留历史语义，不批量抹去关键词；必要时增加下线标记。`upstream.lock.json` 只更新受影响的来源记录，不动 PPT 上游配置。

项目 `.codex/skills/pdf-docx-routing` 若确认只服务独立转换，则移除其技能文件与 agent 元数据；若仍有共享解析指引，先迁到实际材料解析说明中。只处理项目内文件，不修改用户全局 PDF/DOCX 技能或插件。

pyproject/uv.lock 中依赖只有在证明无剩余调用后才删除。MinerU、PDF 文本/图像解析、Word/LibreOffice/Poppler 渲染、DOCX 解析、缓存、Token 设置与取消机制默认保留。不能按包名含 pdf/docx 就移除依赖，也不清理用户配置中的 Token。

## 4. 测试与验收

| 用例 | 必须满足的结果 |
| --- | --- |
| 新安装/正常启动 | 只显示仍支持的创建功能；无转换菜单、表单、失效事件或 import 错误 |
| 旧页签/持久化状态 | 原 pdf 页面状态能回退；无空屏；其他功能选择状态正常 |
| 直接请求旧 skill/任务类型 | 稳定拒绝；模型调用、MinerU 调用和材料预处理均为零 |
| 模型工具清单与旧工具调用 | 不暴露转换四工具；旧调用无法执行；共享读取 PDF 工具仍可用 |
| PDF 材料解析：缓存命中 | 不调用外部服务，仍得到正确 IR、真实来源与图片/表格引用 |
| PDF 材料解析：缓存未命中 | 新适配器路径可执行；既有错误、取消和 Token 隔离保持；有真实环境时使用固定样本验证 |
| 认证缺失、解析失败与取消 | 保持现有稳定错误行为，不以空内容或纯文本降级伪装成功；日志无敏感信息 |
| 简历/DOCX/PPT 消费 PDF 材料 | 各自材料入口可用，至少一条真实代表链路贯通，其余契约与回归覆盖 |
| DOCX/简历导出 PDF | 输出、渲染预览、产物登记及打开不受影响，来源归类正确 |
| 历史转换任务与文件 | 完成/失败/取消记录可读；已有成果可达且内容 hash 不变；重试明确下线 |
| 打包与启动 | 新脚本在打包产物中存在；无旧目录隐式依赖；前端类型检查和构建通过 |

先把专用正向转换测试替换为下线拒绝测试；混合测试文件只移除转换断言，保留 DOCX、渲染、日志安全等用例。针对已迁移的共享脚本保留原有取消、超时、缓存与环境测试，只更新路径或领域名称。不得删除整个混合测试文件来获取全绿。

按项目脚本执行前端类型检查/构建、后端针对性测试与相关回归；最后在新版 UI 手动或自动验证入口、材料上传、历史成果。实际 MinerU 不可用时分开报告已跑的契约测试与未跑的外部解析验证，不把 mock 通过当真实转换通过。外部调用遵循现有项目授权和测试配置，不新增供应商或费用安排。

完成标准：新转换任务无可执行路径；共享 PDF 材料解析及导出未回归；历史文件零删除、零无意改动；专用文件清理完成且无悬空引用；剩余 `pdf_docx_routing`/`pdf_to_docx` 引用仅属于有说明的 retired-id、历史兼容或证据。

## 5. 交接、并行边界与回滚

本任务由单个实现者负责，保持与简历 P2 修订独立。涉及 `runtime.py`、`llm_tools/dispatcher.py`、`sidecar.py` 等共享文件时，先核对最新工作区并协调编辑时段；不得同时整体替换共享文件。实现者不自动启动其他 agent。

建议分为三个可审阅变更：A 共享解析适配器迁移及等价验证；B 新任务下线与历史结果接入；C 专用文件/依赖/说明清理和全链验收。最终交付同时包含三部分，不能只隐藏按钮就宣称完成。每部分列明修改、删除、迁移清单及测试证据。

回滚只反向撤销本次变更，保留他人未提交修改；禁止 reset 到旧提交。A 保持共享解析接口与缓存格式不变，使 B/C 回滚无需恢复用户数据。执行前记录旧产物 hash 和工作区差异；实施后核对历史成果仍存在。不要为了回滚额外复制大量用户材料。

交接指令：阅读本计划、AGENTS.md 与 personal-engineering；先核对共享依赖并完成 D0，按 D1→D4 执行，保护现有未提交修改、用户材料与历史成果。遇到必须删除共享能力、做不可逆数据迁移或改变解析供应商的情况，停在该决策点提交具体方案；常规局部编辑和已授权测试自行推进。提交实际变更、测试与 UI 验收证据后，由用户指定的 reviewer 复核。

## 6. 本次规划的证据范围

已检索并读取入口映射、Sidecar 历史归类、`MaterialService._parse_native_pdf`、MineruService 调用及缓存版本引用，并定位相关专用 Service/工具/测试。未穷尽全部打包或数据库路径；D0 必须继续核实历史加载与脚本打包，不把这里的候选删除清单当作已验证的最终清单。

本轮只新增本计划，不运行转换、不删除代码、不改数据、不创建独立任务。简历 P2 工具闭环评审另行继续，其未通过项不因本计划而改变。

使用模型：GPT-6
