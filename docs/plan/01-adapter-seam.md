# 01: 保留 CLI 行为并引入可切换运行时入口

**What to build:** 用户继续通过现有 CLI 提问、保存和加载会话，并能看到实际选择的引擎；为后续接入提供单一运行时入口。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] FinAgent 对外 run/stream_chat/save/load/clear 行为保持兼容，现有 LangGraph 作为显式旧引擎。
- [ ] 一次请求只进入一个引擎；启动信息显示实际引擎。
- [ ] 复用原有九项集成测试，补充旧调用者兼容性验证。

验证边界：现有 CLI → 运行时选择 → LangGraph 提问及会话保存/恢复；本任务不要求尚未接入的 Harness 可运行。仅使用隔离测试配置与数据；完成本任务不等于已通过最终发布门禁。

