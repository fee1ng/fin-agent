# DSH 链路可观测：社区与标准来源核对

调研日期：2026-11-16。仅阅读本地配置、官方源码与各项目作者仓库；未安装插件、未执行部署。以下区分已核实能力与集成建议，兼容性尚未通过本项目运行验证。

## 项目基线

`requirements-harness.txt` 同时固定 SDK/runtime 为 `0.2.1rc2`；`fin_agent/agent/harness_profile.py` 记录 upstream commit `687f282e8625c6b8d11ff58211a90524be2e4317`，使用 `sdk-minimal`、`dsh-llm-pi-ai` 和 stdio `dsh-mcp-client`。模型调用发生在 Harness runtime，不应假定给 Python LangChain 安装回调即可覆盖。

## 官方采集边界

- [固定 commit 的 session-telemetry](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-telemetry/README.md)：有 `live` 与 `on-demand` 捕获模式，保持 canonical session log，导出副本可通过 `session-telemetry/record` waterfall 脱敏；同一 context 只能注册一个 backend。`emit` 必须非阻塞；交付是 best effort，崩溃时队列可能丢失。
- [固定 commit 的官方 OTel backend](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-telemetry-otel/README.md)：导出 **logs**，默认 `FEEDBACK_ONLY`，显式反馈才释放相应日志前缀；`DISABLED` 不建立传输；此版本拒绝 `FULL`。因此不能把官方插件等同于每请求实时 trace，也不能把 logs endpoint 直接当 Langfuse traces endpoint。
- [固定 commit 的 LLM service](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm/README.md) 与 [源码事件定义](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm/src/index.ts)：`llm/stream` 是可包裹流的 waterfall；单次 stream 为一次 provider attempt，辅助 title/compaction 调用也走该 service。建议用它补模型 attempt 耗时与首个有效输出时间；只读透传，不修改 deep-frozen 请求。

## 社区插件结论

| 项目 | 一手资料确认能力 | 本项目处理建议 |
|---|---|---|
| [CodePrometheus/dsh-observability](https://github.com/CodePrometheus/dsh-observability) | turn→trace、step/tool→span；usage、首 chunk 时间、错误状态；稳定派生 ID；异常终止补闭合；OTLP/HTTP traces | 可作为投影实现参考；优先选型修订见文末云厂商方案补充，不能直接宣称兼容 |
| [PerryLink/dsh-fast](https://github.com/PerryLink/dsh-fast) | session 加载、spill、compaction、上下文注入量、LLM cache 诊断，命令/工具报告 | 性能诊断补充，不替代跨进程 trace；其 README 的验证版本和项目 pin 不完全相同 |

`dsh-observability` [package.json](https://github.com/CodePrometheus/dsh-observability/blob/main/package.json) 为 `0.1.0`、MIT；peer dependencies 精确固定 DSH `0.1.0-rc.6`、Cordis `4.0.1`，不同于本项目 `0.2.1rc2`。其旧 README 的 FULL 说明不能覆盖新版官方行为。

该插件 [README](https://github.com/CodePrometheus/dsh-observability/blob/main/README.md) 明确无 durable outbox、无内置脱敏、未拼接 subagent lineage，且必须替换默认 telemetry backend；稳定 ID 并不自动保证任意后端存储幂等，仍需验证目标后端重复写入行为。

阅读 [projection.ts](https://github.com/CodePrometheus/dsh-observability/blob/main/src/projection.ts) 可见：step 包含模型请求及其工具；usage 由 `assistant/message` 写到 step；未知事件只变成 span event。故进一步开发应拆开纯模型 attempt 和 step，明确失败/重试 `assistant/attempt` 的消耗，不把 step duration 叫模型耗时；不直接复用旧事件 payload 解构。

## Token 语义必须适配

[DSH TokenUsage 类型](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm/src/types.ts) 明确：`inputTokens` 是未缓存输入，与 `cacheReadTokens`、`cacheWriteTokens` 不相交；计费输入为三者之和；`totalTokens` 是可选的权威聚合值。

[OTel GenAI inference 约定](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/client-inference.md) 的 input 包含缓存输入，output 包含 reasoning。因此建议：

1. 保存原始 usage 和来源；规范化 input 为 DSH 三类输入之和；缺失值不能伪造为已知零。
2. reasoning 作为 output 明细，不重复加进总数；适配器的实际输出含义仍需 fixture 校验。
3. 每个 attempt 的最终 usage 只结算一次；stream usage、durable settlement、SDK 通知和重放不得重复累计。
4. 失败 attempt 有 usage 也纳入；没有 usage 的中断标记 unknown，不自动算零。
5. GenAI 约定已迁到 [独立仓库](https://github.com/open-telemetry/semantic-conventions-genai)；固定 schema 版本并做字段转换，避免追随 main 造成历史指标语义漂移。

## 开源方案定位

| 方案 | 官方能力与定位 | 对 DSH 的适配边界 |
|---|---|---|
| [Langfuse OTEL](https://langfuse.com/integrations/native/opentelemetry) | 可接收 OTLP traces；官方页给出 HTTP/JSON、HTTP/protobuf、部署端点以及语义映射要求 | 可作为首选应用层观测后端，但仍要产生 spans、补 usage/metadata 映射；不自动读 DSH logs |
| [Phoenix](https://arize.com/docs/phoenix/tracing/concepts-tracing/how-tracing-works) | 收集并可视化 OTLP traces，可手动或通过 OpenInference instrumentors 埋点 | 可作备选 trace UI；应按其语义映射验证展示 |
| [OpenInference](https://github.com/Arize-ai/openinference) | 基于 OTel 的 AI span 语义与 instrumentation，Phoenix 原生支持 | 与 OTel GenAI 不是同一套字段名，需映射，不能只发同名未知属性就假定完整功能 |
| [OpenLLMetry](https://github.com/traceloop/openllmetry) | OTel instrumentation 集合，覆盖模型 SDK/向量数据库；另有 JS 版本 | 用于实际命中的依赖边界；没有证据支持其对当前 DSH 二进制的完整自动插桩，不能替代 DSH 插件 |

## 建议的验证门槛

在本项目 pinned runtime 的真实 composition 下，用本地 mock 模型与 mock OTLP 接收器验证：正常/多工具/并行工具/模型失败/取消/失败重试/辅助模型调用/重放/崩溃补偿；核对父子 span、真实时间边界、token 不重复、敏感字段脱敏、单 backend 注册与 exporter 故障不阻塞业务。

若目标是每个请求均可追溯，建议所有请求保留最小索引及状态；采用独立持久 outbox 与已确认 canonical 日志补录。该方案为进一步开发建议，不是上述插件已有的可靠性保证。

## 2026-11-16 补充：云厂商维护的 DSH 专用方案与选型修订

前一轮遗漏了云厂商维护的 DSH 专用插件。补充后的优先顺序为：通用 OTLP 路线先验证 LoongSuite；已有腾讯云 CLS/火山 TLS 基础设施则优先核对对应官方插件。CodePrometheus 方案降为投影参考与备选，不再作为首选。

| 方案 | 已核对的一手能力 | 版本与边界 |
|---|---|---|
| [LoongSuite DSH plugin](https://github.com/loongsuite/dsh-plugin) | 独立插件 `@loongsuite/dsh-plugin`；原生 session/agent loop/LLM/tool 生命周期转 OTel GenAI traces 与 metrics；每次真实模型 attempt 独立 span；通用 OTLP/HTTP protobuf；正文默认不采集 | README 声明 `>=0.1.0-rc.6 <0.2.0`，完整验证 `0.1.0-rc.6`；未覆盖本项目 `0.2.1rc2`；支持关系属性不代表已拼接 MCP 内部跨进程 trace |
| [腾讯云 AgentObs DSH 接入](https://cloud.tencent.com/document/product/614/136048) | `tencentcloud-agentobs-sdk-dsh` 把 DSH 生命周期转 Agent Trace；官方文档包含任务/step/model/tool 父子结构、耗时、TTFT、input/output/cache tokens、失败重试与多轮会话关联 | 本次读取的文档（页面更新时间 2026-11-11）仍要求 DSH `>=0.1.0-rc.6 <0.2.0`，不能作为项目 pin 的兼容证明；面向 CLS AgentObs |
| [火山引擎 TLS：DeepSeek Harness 观测](https://docs.volcengine.com/docs/torch-log-service/deepseek-harness-observability?lang=zh) | 官方提供 DSH 日志应用接入、`@volcengine/tls-observer-dsh-install` 安装器；字段包含 span 父子关系、耗时、token、TTFC 和会话关联，使用 TLS/AgentLoop 后台 | 插件系列为 `tls-observer-dsh`；此次未核实当前发布包对 `0.2.1rc2` 的兼容范围，也未验证 MCP 服务内部覆盖 |

LoongSuite [package.json](https://github.com/loongsuite/dsh-plugin/blob/main/package.json) 当前为 `0.1.3`、Apache-2.0。其私有 OTel provider 不替换全局 provider；不导出 OTel logs，可以与日志 exporter 共存；热加载后从下一个 turn 开始采集，不补历史。以上是项目 README 声明，尚未经本仓库运行验证。独立插件不要求 Pilot，也不绑定阿里云后端。

[阿里云官方实践文章](https://www.alibabacloud.com/blog/deepseek-harness-a-practical-guide-to-comprehensive-observability_603771) 区分独立 `@loongsuite/dsh-plugin` 与 LoongSuite Pilot 两种方式；[云监控官方文档](https://help.aliyun.com/en/cms/cloudmonitor-2-0/ai-application-access-ai-coding-agent/) 描述 Pilot 自动发现 Agent、部署采集 hook/plugin 并统一上报，支持 DSH YAML patch/local JSONL 路线。对当前嵌入式 `sdk-minimal` 服务，建议先验证独立插件挂载；多种桌面 Coding Agent 集中治理时再评估 Pilot。

修订后的 P0：固定插件源码版本，对项目实际生成的 `finance.patch.yml` + runtime 进行兼容适配；验证两轮模型、一次 MCP 工具、失败/取消及 usage；通过后再决定复用多少采集代码。仍需独立补 Python 宿主入口、stdio MCP context 传播、金融工具内部 I/O、可靠导出和原生 checkpoint 关联。不能因为云平台有 DSH 接入教程就承诺这些边界自动齐全。
