# 02: 真实 Harness 完成一次可持久化的只读工具提问

**What to build:** 真实 Harness 完成一次可持久化的只读工具提问，使用户能通过一次真实提问及公开查询验证这条端到端路径。

**Blocked by:** 01 — 保留 CLI 行为并引入可切换运行时入口

**Status:** in-progress

- [ ] 锁定官方 SDK/runtime 和 profile 版本与校验值，在 Linux/Python 容器可复现启动。
- [ ] 保留当前 deepseek-chat 配置，验证固定版本的模型协议及公开兼容路由；不得静默更换模型。
- [ ] 裁剪 sdk-minimal 的 shell、图片及目录工具，只经本地 MCP 暴露一个项目只读工具。
- [ ] 真实模型协议替身驱动原生 Harness 调用该工具；关闭桥时明确失败。
- [ ] 原生会话结果 flush 后才报告完成，重启可读；若上游没有公开屏障则补受支持插件。

验证边界：应用提问入口 → 真实 Harness 与本地工具桥 → 持久化记录 → 用户可见结果或轨迹查询。仅使用隔离测试配置与数据；完成本任务不等于已通过最终发布门禁。

当前证据见 [兼容性验证记录](harness-compatibility-evidence.md)；剩余门禁未通过前不标记完成。

