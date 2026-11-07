# Web 实现与本机部署验收

日期：2026-11-07（北京时间）。范围：现有 fin-agent 的只读 Web 问答、持久化 SSE、独立 Worker、管理员诊断及数据恢复。用户明确本轮以本机 HTTPS 验收交付，只需保证本地连接；内网配置保留为备用，不要求内网或外网开放。七项计划全部完成。本报告不将 localhost 自签证书视为机构浏览器信任配置。

## 交付核对

| 计划 | 结果与证据 |
| --- | --- |
| 01 只读 Agent 与观测 | 工具 schemas 和执行边界双重白名单；Web 提示词不读全局画像；模型与实际工具各自计时，保留关联 ID，业务失败准确标识 |
| 02 登录与隔离会话 | Argon2、服务端 Cookie、Origin/CSRF、限速及撤销；两普通账号的 HTTP 与实际浏览器会话隔离均通过 |
| 03 后台问答与推送 | LangGraph 实际调用 DeepSeek；两路并发；SSE 在运行终态之前收到正文；最终回答、上下文和终态同事务提交 |
| 04 重连与恢复 | 浏览器生成期间刷新，恢复后共两问题两运行；真实 SSE 主动断开后继续接收；强杀 Worker/API 重启及写入失败有进程测试 |
| 05 管理员诊断 | 真实浏览器按会话、工具筛选，仅列该会话两次运行；详情展示两模型一工具、父模型与 tool_call_id、最长调用及结果摘要 |
| 06 保留与备份恢复 | TTL 独立清理、快照回退、级联删除、外部删除日志；真实容器内一致备份、跨容器拒绝在线恢复、停机恢复不复活删除或登录 |
| 07 部署 | Docker Desktop 上的独立镜像、卷及同源 HTTPS 服务健康；本机验收通过，用户确认仅本机交付，内网配置保留 |

## 自动化及构建

- Windows：`python -m unittest discover -s tests -v`，70 项，69 通过、1 跳过。日志 `.local/web-backend-tests.log`。原生 Harness SDK 未安装导致其专用集成用例跳过，不能声称原生 SDK 真连接已验收；已有 CLI/Harness 协议及恢复回归通过。
- Linux：新镜像在 `docker.exe --context desktop-linux run --rm --init --network none ...` 中执行同样 70 项，69 通过、1 跳过。日志 `.local/web-linux-tests.log`。强杀测试触发 resource_tracker 清理残留信号量的警告，隔离容器在退出时移除；不将其计作正常运行性能数据。
- Windows 最终进程回收变更后，恢复测试 10 项再次通过：`.local/web-recovery-final.log`。
- 前端：`pnpm test`，13 项通过；`pnpm build`（包含 TypeScript 检查）通过。Chat/Admin 按路由拆包，首屏约 685 kB/gzip 228 kB，仍有 Vite 体积提示，首版少量用户范围内保留此限制。jsdom 的文本框布局 NaN 提示在真实浏览器未出现，实际页面已检查。
- Docker 后端所有 Linux/Python 3.12 依赖使用 `requirements-web.lock` 哈希锁；前端 frozen pnpm 锁；三个基础镜像按摘要固定。`pip check` 通过。
- wheel 构建、重新安装到隔离目录、离开源码目录导入和迁移检查通过，版本 `0005_retention`；原 Dockerfile 的打包输入补充 requirements-web.txt，避免新增 extra 破坏原 CLI 构建上下文。

## 真实 HTTPS 供应商验收

最终报告 `.local/fin-agent-web.acceptance.json`，脚本 `deploy/web/verify.py`。测试客户端显式信任 `.local/web-tls/server.crt`，未关闭 TLS 证书校验；Cookie 的 Secure/HttpOnly/SameSite=strict 均检查通过。浏览器 QA 使用单独的回环 HTTP 项目，不绕过浏览器证书警告。

最终两次运行 `5daf0fe2f2d44d0b9a753b21ac3a1728` 和 `a7e316c0dd214658b6f4d8d88eee4e09` 均完成，两执行时间区间确实重叠，各有 2 次模型调用、1 次 `get_current_time` 工具调用及 23/20 个正文片段。两次均在服务端完成时间之前收到正文，并各主动断开 SSE 一次后补发。问题仅查询时间和方法说明，没有股票交易或金融状态写入。

| 指标 | 两次实际值 |
| --- | --- |
| 排队 | 24.3 / 39.5 ms |
| 执行 | 6.33 / 5.81 秒 |
| 模型调用总耗时 | 4.18 / 3.65 秒 |
| 工具耗时 | 约 0.12 / 0.14 ms |
| 执行减去模型与工具耗时 | 约 2.16 / 2.16 秒，包含隔离子进程启动、初始化和调度，不是纯网络延迟 |
| 在线事件到达 p95 | 146.2 / 142.3 ms，仅 3/4 个在线样本 |
| 重连订阅建立 | 约 2.07 / 2.08 秒；单独记录，不混入在线传输指标 |
| 认证快照请求往返 | 20 次，p50 4.10 ms，p95 4.83 ms |

重连期间积压事件年龄约 2 秒，与主动关闭连接后的订阅重建有关，不能据此归因为模型变慢或代理持续缓冲。额外受控探针 `.local/web-tls-event-probe.log` 观察到 queued 立即到达、started 约 157 ms、正文约 2.47 秒起逐段到达、终态约 3.08 秒到达。由此核验推送发生在执行期间。

受控负载使用独立 `fin-agent-web-bench` 项目、无模型凭据的 scripted factory、两个同时执行的约 1 秒流式模型；报告 `.local/fin-agent-web-bench.acceptance.json` 与真实供应商报告分别保存。默认配置仅说明两路执行、排队上限 16；这组小样本不证明长期吞吐容量。浏览器首帧/事件提交到浏览器绘制的 p50/p95 未采集，不能将 HTTP 到达时间冒充显示耗时。

## 真实浏览器

通过登录/退出、新建会话、真实工具和 Markdown 表格、生成期间刷新恢复、跨账号拒绝和管理员筛选/逐调用展开。刷新后该会话只有两次提问对应的两次运行。最终检查浏览器控制台没有 error/warn。页面截图：

- [问答页面](../.local/web-chat-acceptance.png)
- [开发者调用时间线](../.local/web-admin-acceptance.png)

## 容器恢复与隔离

`.local/web-container-restore.json` 记录实际流程：在线 SQLite 备份后删除一个验收会话；Worker 在线拒绝恢复；停止 Worker 后，另一个容器中的 API 存活文件锁仍拒绝恢复；全部停止后恢复成功，自动生成 pre-restore 回滚备份、重放一条删除日志；重启后旧 Cookie 401，重新登录后已删除会话仍为 404。

部署检查确认 `desktop-linux` 指向 Docker Desktop 的 Linux 引擎。原 `fin-agent:harness-latest` 镜像、`fin-agent-harness-latest` 容器及 `fin-agent-harness-data` 卷保留，未删除、覆盖、迁移或切换 Desktop 后端。原 Harness 容器当前为停止状态，不宣称其业务运行状态已验收。

## 交付边界

主服务当前默认只绑定 `127.0.0.1:8443`；验收证书 14 天有效。正式内网使用前需按 [部署手册](WEB_DEPLOYMENT.md) 配置真实入口、机构信任证书和正式管理员账号。本次不会自动安装系统根证书或绕过浏览器警告。聊天及必要上下文长期保留，备份恢复必须保留最新外部删除日志；定时与异地备份由运维接入现有调度。

结束状态：主项目 API、Worker、HTTPS 入口均 healthy；临时 `fin-agent-web-qa` 与 `fin-agent-web-bench` 服务已停止，卷和证据保留。主项目验收账号及随机密码位于 Git 忽略的 `.local/fin-agent-web.accounts.json`，没有写入本报告或浏览器存储；正式开放前应创建正式账号并停用 qa-admin/qa-alice/qa-bob。
