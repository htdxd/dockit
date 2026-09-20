# DocKit 简历网站：Windows 邀请试用版

## 定位与部署前提

本包提供新建任务、进度/报错、产物导出三个页面。没有供应商设置页、其他文档功能或付款接口。支持上传材料与手动填写同时提交、在进度页补充信息、DOCX/PDF 下载和自愿反馈。只开放已组件化的 t001、t109。

**先买短期试用服务器，不建议未经验证直接购买长期套餐。** 当前两个模板的文字测量与渲染都依赖 Microsoft Word COM，无法直接运行在 Linux 或 Linux Docker 中。网页和 API 跨平台，但完整生成路径仍需 Windows。

建议试用机：Windows Server 2025 Desktop Experience，4 vCPU、16 GB RAM、100 GB SSD、5–10 Mbps 公网带宽，无需 GPU。LLM 和 MinerU 均调用云端 API。Windows/Office 费用需单独核实。资源建议是工程估算，**不是该云机上的压力测试结果**。

Windows Server 2025 对 Microsoft 365 Apps 的操作系统支持到 2029 年 10 月，并不等于支持任意服务端 Word 自动化。微软明确不推荐/不支持传统无人值守、非交互式 Office 自动化，可能遇到阻塞和死锁；无人值守 RPA 还有专用授权条件。购买前请向微软或授权商确认面向外部用户提供此服务的授权适用性。普通个人 Office 授权不能在本说明中被当作已满足条件。

- [Windows Server 与 Microsoft 365 Apps 支持范围](https://learn.microsoft.com/en-us/microsoft-365-apps/end-of-support/windows-server-migration)
- [服务端 Office 自动化限制](https://support.microsoft.com/en-US/Visio/considerations-for-server-side-automation-of-office)
- [无人值守自动化注意事项](https://learn.microsoft.com/en-us/office/client-developer/integration/considerations-unattended-automation-office-microsoft-365-for-unattended-rpa)
- [无人值守授权概览](https://learn.microsoft.com/en-us/microsoft-365-apps/licensing-activation/overview-unattended)

本包先服务受控邀请码试用，**不能据此宣称已达到公开商业网站的可靠性或完成授权审查**。正式扩大规模前，需要独立 Windows 渲染工作节点，或者验证可替代 Word 的服务端排版引擎。

## 需要先准备什么

1. 短期 Windows 云机/VM，能安装和启动 Word，有独立普通用户桌面会话。首次登录完成 Office 激活、隐私确认等界面。服务进程不要以管理员身份运行，也不要将 Word 工作进程放进 SYSTEM/Session 0 服务。
2. 与本机模板一致的字体，尤其微软雅黑、宋体、Calibri 等实际用到的字体；核实字体许可。字体缺失会影响换行，不能仅凭程序启动就判定质量一致。
3. 稳定的 LLM API URL、模型和 Key，必须能调用工具并真实识图；64K 输出额度是否可用以供应商为准。先运行能力探测，再做真实任务。推荐优先使用此前已验证稳定的 Qwen 模型；不要只依据模型名称判断能力。
4. MinerU API Token、余额/配额和可用网络。本包带原生 CLI，但不含本地 MinerU 大模型。DOCX/PDF 会发送到 MinerU，相关内容和图片会发送到 LLM；先核对所选供应商的数据处理条款。
5. 域名和 DNS、HTTPS、管理员联系方式。若选择中国大陆节点，购买前向云厂商确认备案及接入要求。确认云机能够正常访问两个 API，勿仅在自己电脑测试。
6. 每人一个邀请码，不要公开发布共用码；同一码在不同浏览器登录会访问同一份任务历史。配置文件和 data 目录仅供服务账户及管理员访问。

## 解压与配置

解压到例如 `C:\DocKit-Web`，避免放进 IIS/Caddy 的公开静态目录。包内含 Python、第三方依赖、MinerU CLI、Poppler、简历模板；无需另装 Node 或 Python，**不含 Word 和任何真实密钥**。

复制 `server.example.json` 为 `server.json`，修改：

- `public_origin`：最终网站地址，例如 `https://resume.your-domain.com`，不带路径。
- `provider.kind`：`openai_compatible`、`openai`、`openai_responses` 或 `anthropic`；填写匹配的 URL、模型与 key。
- `mineru_key`：MinerU Token。
- 正式部署保留 `secure_cookie: true`；仅本机测试可设 `public_origin: http://127.0.0.1:8080` 和 `secure_cookie: false`。
- 默认数据位于包目录的 `data`；需要迁移磁盘可修改 `data_dir`。

“内置供应商”指服务器统一配置、用户无需配置，不是把 Key 写进网页或发布 ZIP。不要把实际 `server.json` 提交仓库、公开下载或放进静态目录。

在命令提示符中执行：

```bat
cd /d C:\DocKit-Web
start-web.bat --check
start-web.bat --probe
start-web.bat --invite 10 --quota 5
start-web.bat
```

`--check` 不需要填写密钥或先创建 server.json，也不调用付费 API。它检查 COM 实际注册程序（排除 WPS 兼容接口）、Word 版本与程序目录，再对 t001/t109 实际测量正文及个人信息、导出 PDF、校验替换文字并生成 PNG。完整结果保存为配置文件所在目录的 `environment-check.json`；失败时请提供此报告。Word 2007（12.0）不支持当前文本框结构，建议采用已验证的 Word 16.x 系列。版本号本身不代表环境可用。

`--probe` 则调用真实模型验证工具/视觉/强制工具能力，并把报告写入服务端配置。工具或视觉未通过时不要开放试用。探测可能产生少量 API 费用，未验证的强制工具能力保持不启用。MinerU Token 是否有效仍需一次真实文档解析验证。环境检查不证明云端凭据有效，也不替代观感验收。

邀请码只在创建时打印原文，数据库保存哈希。每码默认 5 次，可用 `--quota` 调整。失败和取消返还使用次数，但对应 API 调用仍可能计费；邀请码制度本身不是支付系统。

程序默认只监听 `127.0.0.1:8080`，浏览器用户应通过 HTTPS 反向代理访问。`--check` 通过不代表 Word 在断开远程桌面后一定正常，应按下方清单实测。维持合法的独立交互会话；退出登录可能中断工作。若配置登录后启动，使用该普通用户的登录触发计划任务，不要直接改成 SYSTEM Windows 服务。

## HTTPS 与网络

自行从 [Caddy 官方下载页](https://caddyserver.com/download) 下载 Windows Caddy。复制 `Caddyfile.example` 为 `Caddyfile` 并替换域名；将域名 A 记录指向服务器。

```bat
caddy validate --config Caddyfile
caddy run --config Caddyfile
```

公网只开放 80/443；8080 保持本机监听。远程桌面仅允许管理员来源 IP。Caddy 转发到本机 API，不能将整个包目录作为文件服务器发布。Caddy 的证书签发需域名正确解析、80/443 可达。

## 队列、容量与成本

- 正式版默认 1 个生成任务运行，最多 20 个排队；每个邀请码最多 1 个未结束任务。`max_concurrent_tasks` 可配置，但应先用整机实测确定稳定档位，详见 `LOADTEST.md`。
- 多人可同时上传、查看、下载。API 保持单进程，由内部队列调度独立生成进程；不要添加 Uvicorn `--workers`、开启 reload 或同时启动多个网站进程共享 data。
- 补充信息仍占用当前任务槽，避免把 Word 与模型状态转存成另一套恢复协议；30 分钟总时限包含等待回答时间，超时失败返还次数。排队等待不计入这一时限。
- 单份平均 3/5/10 分钟时，理论每小时 20/12/6 份；这些是算术示例，非实测。API 429、重试、材料大小和用户回答会降低吞吐量。
- 模型正式调用复用现有 SDK 重试策略：只对可重试错误最多重试 3 次。失败后不会自动无限重新生成。
- 暂不建议以“1 元一次”承诺长期收费。先通过实际任务记录统计单次模型费用、MinerU 费用、失败率、人工排障成本及服务器月费摊销，再设置价格和退款规则。

## 文件、隐私与日志

- 最多 6 份材料、总计 30 MB；PDF 不超过 20 页；只接收页面列出的格式。DOCX 宏拒绝，压缩展开体积及图片像素也有上限。
- 每个任务的上传、日志、成品在 `data/jobs/<任务编号>/`，不会直接作为静态网站提供。下载需要所属邀请码会话并且任务已完成。
- `events.jsonl` 保存事件（按配置密钥脱敏）；`outputs/.skill-toolbox-logs/` 保存详细运行日志；`stderr.log` 保存任务进程错误，`server-error.log` 保存调度异常。全站运行错误在 `data/logs/server.log`，按体积轮转。
- `website.sqlite3` 保存邀请码哈希、会话、队列状态及反馈。材料、任务记录和对应反馈默认在提交 7 天后自动清理，清理每小时检查一次，活跃任务跳过；会话 7 天有效。运行错误日志另按体积轮转，备份由管理员单独管理，不能声称备份也自动清理。
- 任务中断后会明确标记失败并返还次数；排队任务可在网站重启后继续。没有对进行中的 LLM/Word 会话自动断点续跑。
- 用户在进度页看到任务编号和错误，可以提供编号给管理员；日志不开放匿名下载。原始上下文和日志可能含简历个人信息，限定访问权限，不应直接公开分享。
- 现版本没有付费、注册找回、管理员网页、用户主动删除入口、多节点调度。需要删除个人数据时由管理员处理对应任务目录及 SQLite 记录；正式公开运营前补齐用户条款、隐私说明与联系渠道。

## 上线验收（在实际服务器执行）

1. `--check` / `--probe` 通过，确认浏览器中没有供应商设置和 API key。
2. 分别用 t001/t109 从手动经历生成 DOCX，下载并核实内容和排版；再生成 PDF。
3. 上传 DOCX、PDF、图片及 TXT，多文件合并提交；验证 MinerU 网络、照片比例和页面质量。
4. 确认补充信息能继续，取消后下一任务能运行；刷新或关闭网页后能够凭同一码返回。
5. 用两个邀请码分别建立任务，确认彼此列表与下载不可见；验证错码、超额和不支持文件被拒绝。
6. 在队列中重启网站，确认排队/中断状态符合上文；断开管理员远程桌面后再次生成，排查 Word 阻塞。
7. 验证公网 HTTPS 和防火墙；确认错误日志确实落盘。按真实耗时和 API 用量决定第一批发放多少邀请码。

本机测试与实际服务器验收必须区分：本包准备时可以验证 Web 接口和本机 Word 路径，但不能代替未购买云机上的稳定性、授权、带宽或无人值守压力测试。

## 源码重新打包

在项目 Windows 开发环境中执行 `uv run --extra web scripts/package_web.py`。构建机需要现有 Word/Poppler/MinerU CLI 和用于解析 DLL 依赖的 objdump。输出 `backend-dist/DocKit-Resume-Web-Windows.zip`，只打包运行资源和部署文件白名单；不包含本机真实配置、用户数据和旧任务日志。
