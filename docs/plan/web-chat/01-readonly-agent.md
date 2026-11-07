# 01: Web 只读 Agent 与准确调用追踪接缝

**What to build:** 通过现有 Agent 接口执行只读金融问题，并在实际调用边界获得模型与工具观测，为用户问答提供安全隔离的执行基础。此项为先行的最小兼容性重构。

**Blocked by:** None (can start immediately).

**Status:** complete

- [x] 现有 CLI 默认行为及运行时选择回归通过。
- [x] 可注入工具 schemas、执行器、提示词和观测器，不复制整份 Agent。
- [x] Web 明确工具白名单；越权工具调用在执行层被拒绝，不读全局画像。
- [x] 模型调用及同名多工具逐次计时，业务失败被识别；工具开始在真正执行前发出。
- [x] 通过受控模型和工具验证上述行为，无需真实凭据。

证据：本地项目虚拟环境执行 `python -m unittest tests.test_web_agent tests.test_runtime_selection tests.test_branch_merge tests.test_trace_storage tests.test_harness_flow tests.test_harness_recovery -v`，31 项，30 通过、1 项原生 Harness SDK 缺失跳过。新 Web Agent 测试 7 项全部通过。尚未进行真实模型或 Docker 验收。
