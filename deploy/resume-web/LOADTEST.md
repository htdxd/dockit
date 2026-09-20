# Windows 整机压测

## 两个入口

`start-web.bat` 是正式邀请码版；`start-benchmark.bat` 是仅本机压测版。两者使用同一套桌面风格页面和生成逻辑。压测版自动建立独立浏览器会话，不需要发码、不限制次数、允许一个会话提交多个任务。文件大小和单任务超时限制仍保留；并发由参数指定，不会无限拉起 Word。

先配置 `server.json` 的模型和 MinerU Key，安装并激活 Word、字体，运行 `start-web.bat --check` 和 `--probe`。不要在公网反向代理中开放压测入口。不要把压测配置覆盖正式站正在使用的配置。

也可以先不填密钥执行 `start-web.bat --check`：它会实际验证两套模板的测量、PDF 导出及 PNG 渲染，报告写入 `environment-check.json`。**仅装 WPS 不符合要求，兼容接口报告的 COM 12.0 不能证明 Microsoft Word 已安装。** 请先通过环境检查再运行生成压测，避免把环境错误误当作并发瓶颈。

手动试用不限次数版本（另选端口，避免与正式站冲突）：

```bat
start-benchmark.bat --port 8081 --concurrency 2
```

浏览器打开 `http://127.0.0.1:8081`，不显示邀请码弹窗。默认仍使用 `server.json` 的 data 目录；若需完全隔离，复制配置并修改 data_dir，传 `--config` 指向副本。

## 自动压测

脚本自动为每档启动独立本机服务器、独立数据库及任务目录。**不需要发码，也不依赖手动启动的网站。** 测试结束只清理自己启动的服务器进程树，不会按名称杀掉所有 Word。

先查看计划，不会调用 API：

```bat
stress-test.bat --mode generate --levels 1,2,4 --rounds 2
```

网页读取请求压测，不生成简历、不调用 LLM/MinerU：

```bat
stress-test.bat --mode http --levels 1,8,16,32 --rounds 100 --execute
```

真实生成压测，**会产生供应商费用**：

```bat
stress-test.bat --mode generate --levels 1,2,4 --rounds 2 --execute
```

上例共 14 份简历，t001/t109 交替，输出 DOCX+PDF，使用合成经历；自动对补充问题选择无更多信息。它测的是生成服务能力，不包含真人回答延迟。程序下载成品、检查文件可解析及每任务独有姓名标识，发现内容串单也判失败。

确认前三档稳定后再扩大样本或加到 8 并发；不要因为有 64 GB RAM 就直接运行 32 个 Word 任务。`--rounds 10` 表示每档每个并发用户运行 10 个任务。

需要把 MinerU 解析也纳入真实生成压测时，使用你有权上传的测试文件：

```bat
stress-test.bat --levels 1,2,4 --rounds 2 --material C:\test\sample.pdf --execute
```

上传材料应与合成姓名无冲突，或接受“姓名标识未找到”被报告为内容失败。默认不上传材料，因此默认真实生成压测不覆盖 MinerU 服务吞吐。可通过 `--template t109` 或 `--template t001` 单独测试，避免模板成本差异干扰结论。

## 报告怎么看

报告位于 `loadtest-results/时间戳/`：

- `summary.csv` / `report.json`：每档成功率、成功任务 P50/P95、吞吐量、排队耗时、实际生成耗时、峰值整机 CPU、最低可用 RAM、峰值 Word 进程数。
- 每档 `results.csv/json`：各任务的编号、耗时、失败原因及补充问题次数。
- 每档 `resources.csv`：每 2 秒采样的 CPU、RAM、服务器进程树 RSS、整机 Word 数量和 RSS。RSS 为工作集之和，不等同于独占物理内存。
- 每档 `data/jobs/`：完整任务日志和产物，用于分析 Word COM、模型限流和内容问题。可能含测试材料，请不要把整个目录公开上传。

统计口径：成功吞吐按本档总墙钟时间计算；P50/P95 仅统计成功任务，必须结合失败率判断，不能用“失败得快”推断更快。HTTP 模式报告的吞吐单位同样为每小时请求数，不是每小时简历数。两轮只是冒烟测试，不足以确定可靠 P95。

建议关闭该机器其他 Word 文档和重负载程序，使用相同网络、模型、模板与样本进行对照。正式站并发值应低于出现明显失败/长尾的档位，留资源余量。高并发导致 API 429 时增加 RAM 通常没有帮助；出现 Word COM/文档串单时也不能仅靠加内存解决。

脚本不自动重试整个失败任务，模型内部原有的可重试错误策略保留。下载验证不替代观感验收，应每档抽查两模板成品。正式站默认并发仍为 1，压测结论出来后再修改 `max_concurrent_tasks`。Uvicorn 仍使用单进程，不能用其 workers 参数替代这里的任务并发。
