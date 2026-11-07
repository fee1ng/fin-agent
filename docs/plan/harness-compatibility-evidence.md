# Harness 兼容性验证记录

验证日期：2026-11-03。此记录是任务 02 的部分证据，不代表完整功能或部署通过。

## 已验证

- 本机 Docker Desktop `desktop-linux` 中，独立开发容器使用 Python 3.11；SDK 与 runtime 均为 `0.2.1rc2`。
- 对应上游源码 commit 为 `687f282e8625c6b8d11ff58211a90524be2e4317`。
- `python -m tests.harness_probe` 使用真实 Harness、真实 MCP 和本地模型 HTTP 替身完成两次模型请求及一次项目时间工具调用。
- 官方默认 DeepSeek 适配器使用 `/messages`；项目通过官方 `llm-pi-ai` 的 `openai-completions` 路由保留 `deepseek-chat` 模型名。当前只验证协议替身，真实 DeepSeek 连通性仍待最终 smoke。
- 模型可用工具严格为 `mcp__finance__get_current_time`，shell、文件工具和 MCP resource 工具未暴露。
- 原生请求不包含 `dsh_session_log` 或 `dsh_plugin_packages`。
- 自定义 profile 插件使用公开 `llm/stream`、`tools/execute` 和 `sessions.flush` 接口，在开始执行前等待原生持久化；`turn/end` 后写入 fsync 的确认序号。
- 宿主等待最终确认后才返回结果；测试在 SDK 正常关闭之前读取原生 JSONL，确认答案与 `turn/end` 已存在。
- 重新启动 runtime 恢复同一个原生会话成功，恢复过程未请求模型。
- `python -m unittest discover -s tests -v`：12 项通过（9 项已有集成测试、3 项运行时选择测试）。

## 尚未通过的门禁

- SDK/runtime wheel、profile 和依赖完整来源摘要锁定。
- 禁用 MCP 后失败的负向验证、磁盘满及 flush 失败注入、强杀恢复测试。
- 应用逐提问身份关联、全通道持久化前脱敏、完整金融工具桥。
- CLI 会话兼容、离线索引与导出、容量管理和性能测试。
- 真实 DeepSeek 请求、独立审查修复、生产镜像构建部署与回滚演练。

开发验证没有切换生产容器、修改业务卷或替换现有运行镜像。
