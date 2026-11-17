# 基于 DSH 的 Agent 请求级可观测开发方向

调研日期：2026-11-16。本文是开发建议，未安装插件、未运行或部署 Docker，也不表示已验证生产链路。社区方案的一手来源与适配限制见 [调研记录](agent-observability-community-research.md)。

## 1. 结论与范围

建议沿用 DSH（DeepSeek Harness）作为执行事实来源，优先验证 LoongSuite 的 DSH 专用插件并补薄层适配，以 OpenTelemetry traces 统一 Python 宿主、DSH runtime、MCP 和金融数据调用；无既有云观测平台时优先用 Langfuse 提供 Agent 调试与用量界面，Phoenix 为备选；已有腾讯云 CLS 或火山 TLS 时先评估对应官方插件。需要运维级跨服务排障时，再接 Tempo / Prometheus / Grafana。云厂商专用方案及版本限制见文末补充。

先解决采集覆盖率、因果关联、真实耗时和用量口径，再选择展示平台。接入平台本身不会自动获得 DSH 的内部步骤。社区插件先做兼容性验证，避免同时启用多个重复生成模型 span 的自动埋点组件。

“内部步骤”定义为可观测执行事实：接收、排队、恢复、上下文准备、模型尝试、工具调用、外部 I/O、结果处理、持久化与终态；并不承诺访问模型隐藏思维链。Web 当前已有内容过滤规则，新增追踪默认延续不采集思维链正文的边界。

## 2. 当前代码具备什么，缺少什么

| 代码位置 | 当前事实 | 下一步 |
| --- | --- | --- |
| `fin_agent/agent/harness_profile.py` | SDK/runtime 锁定 `0.2.1rc2`，上游 commit 为 `687f282e8625c6b8d11ff58211a90524be2e4317`；使用 `dsh-llm-pi-ai` 和 stdio `dsh-mcp-client`；模型路由 `maxRetries=0` | 所有插件先对该版本验证；将 instrumentation/schema/plugin 版本写入运行元信息 |
| `fin_agent/agent/harness.py` | 已有 run/input/session/native turn 关联；实时通知主要用于输入归属；工具事件在 `runtime.run()` 返回后遍历输出 | 增加实时观测适配，不用结束后的输出到达时间推算执行耗时 |
| `fin_agent/agent/harness_checkpoint.mjs` | 已使用 `session/event`、`llm/stream`、`tools/execute` 和 `sessions.flush`；已有持久化屏障 | 在独立插件里采集开始/结束/异常/usage，验证插件顺序，保持现有 checkpoint 语义 |
| `fin_agent/agent/tool_bridge.py` | MCP 调用金融工具、识别业务错误、stdout 专用于协议 | 每次请求恢复 trace context；在真实 execute 和数据源调用边界采集耗时 |
| `fin_agent/agent/trace_store.py`、`trace_query.py` | 原生日志 + 接收日志 + 可重建 SQLite 查询；导出为自定义 `fin-agent-trace` JSON | 增加标准 span 投影、持久化导出游标；现有 JSON 导出不等于 OTLP |
| `fin_agent/agent/web_observer.py`、`fin_agent/web/worker.py` | Web 使用 LangGraph/LangChain 回调采集模型与工具 | 共用观测 DTO，但 DSH 需要独立适配器；不能把 LangChain 回调接上就宣称覆盖 DSH |
| `fin_agent/web/admin.py` | 已有 span 时间线/usage 查询，engine 返回值目前写为 langgraph | 改成实际 engine 元数据，并支持读取 DSH 投影，先复用管理页入口 |

这些结论来自本次源码阅读；历史规格中的“待实现”和历史验收结论不能代替当前运行环境验证。

## 3. 目标调用链

```text
agent.run                         一次被接受的提问，一个 trace
├─ queue.wait                     排队
├─ runtime.initialize/resume      当前每次运行都会进入 runtime 上下文，单独衡量其开销
├─ agent.turn
│  ├─ agent.step 1
│  │  ├─ llm.attempt 1            模型、TTFT、usage、finish reason
│  │  └─ tool.execute A           tool_call_id；关联触发它的 llm span
│  │     └─ mcp.client → mcp.server
│  │        └─ finance.execute
│  │           ├─ datasource.http
│  │           └─ normalize/compute
│  └─ agent.step 2
│     └─ llm.attempt 2            最终回答
└─ checkpoint.final               持久化确认
```

采用真实执行的父子层次；模型调用已经结束后才执行的工具，可以与模型作为 step 的兄弟 span，通过 span link / `triggered_by_span_id` 表示因果。不要为了树形展示把已经结束的模型 span 人为拉长。tool、MCP、HTTP 是不同层次的耗时，汇总时不能相加重复计算。

每个 span 最少保存 `trace_id`、`span_id`、`parent_span_id`、`run_id`、engine、kind、开始/结束、状态。DSH 扩展字段保存 `harness_session_id`、turn、step、attempt_id、tool_call_id、tool_execution_id、native_seq 区间、profile hash、数据源和版本。

一个 session 可以有多个 run/trace；用户主动重试创建新 run/trace，用 link 关联原 run；模型内部重试在同一 trace 下建立独立 attempt。幂等请求再次读取旧结果不能新增一次模型调用费用。

## 4. 优先开发的六个方向

### 4.1 DSH 原生采集适配器

新增 `harness_observability.mjs` 和 `fin_agent/observability/`，职责是生命周期捕获、事件标准化、span 身份映射和输出。复用现有公开 hook；优先验证社区 DSH 插件能否覆盖，否则实现最小适配器。

- `llm/stream` 包装实际流迭代，采集开始、首有效 chunk、结束、异常、取消；stream 创建耗时不等于消费完成耗时。
- 此 service 也覆盖 title/compaction 等辅助模型调用：属于请求的挂到对应 run，启动/后台调用建立独立 trace 并关联来源，不能只计算最终回答而遗漏实际费用。
- `tools/execute` 包装真实执行，记录业务失败、异常、超时和未知结果；`tool/call` 只代表提出调用，不能自动视为已经执行。
- session 事件映射 turn/step、原生身份、模型最终 usage 和终态；去重键包含 session、原生序号、事件/attempt 身份。
- 记录 `context_build`、`compaction` 等内部阶段的前提是锁定版本真实暴露该边界；无法观察的阶段标记 coverage，不伪造节点。
- checkpoint 等待单独计时；明确模型端耗时是否包含 barrier，避免插件顺序不同导致指标口径漂移。
- 不改写 Harness 原生日志；遥测记录是派生视图，原生执行日志与确认水位仍是恢复依据。

### 4.2 跨进程上下文传播

Python 宿主创建 trace，传给 DSH，再逐请求传到 MCP。当前 stdio 链路不能只依赖 HTTP 自动埋点。优先使用请求 metadata 中的 W3C `traceparent` / `tracestate`；最新 MCP SDK 已描述此方式，但项目固定的 Python MCP 1.30.0 和 DSH 内置客户端必须单独验证，不能照搬新版能力。[MCP 官方 SDK 文档](https://ts.sdk.modelcontextprotocol.io/v2/advanced/opentelemetry.html)

若当前 DSH SDK 提交接口不支持透传，先验证受支持的 runtime 扩展/宿主绑定机制，建立 `run_id ↔ native turn ↔ trace context` 的显式映射；无法打通时展示为关联 trace 并标记断点。长驻进程环境变量不适合携带随每次请求变化的 context；也不要把 trace 字段塞进模型可见工具参数或 prompt。

MCP bridge 每次调用恢复上下文，执行后清理；线程池/异步任务显式复制上下文。用并发会话和同名工具多次执行验证不串链。

### 4.3 Token、耗时与成本口径

| 指标 | 口径 |
| --- | --- |
| 总耗时 | 接收至确认终态，包含排队与最终持久化 |
| 执行耗时 | 真正开始执行至终态，另列 runtime 启动/恢复开销 |
| 模型耗时 | 每次实际模型尝试从开始至流关闭 |
| 首 chunk / 首正文 | 分别记录首有效响应片段、首段可展示正文；工具参数片段不等于正文 |
| 工具/数据源耗时 | 各自执行边界内用单调时钟测量，展示包含时间和独占时间 |
| input/output tokens | 供应商实际返回的每 attempt 用量；缺失为 null，并给出 coverage |
| cache/reasoning tokens | 原样保留细分字段和口径；可能属于 input/output 的子集，不能再次加进 total |
| run token 总量 | 汇总所有实际模型尝试，包括有已知 usage 的失败尝试；累计 snapshot 不重复累加 |
| 成本 | 依据模型、供应商、币种、价格表版本及有效时间估算，区分供应商账单与本地估算 |

同时显示已知 token 小计、usage 完整率和未知调用数。输入上下文在每轮请求中被供应商再次计费时，应按实际每轮计入。模型/工具 span 的嵌套耗时不能直接求和得出 run 时长；并行步骤按时间线/关键路径分析。

采用 `gen_ai.*` 等标准属性并锁定 semantic convention 版本；GenAI 规范仍在演进，保留供应商原始 usage 到内部结构，通过版本化映射导出。[OTel GenAI 官方规范](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)

特别注意本项目 DSH 的用量语义：锁定版本 `TokenUsage.inputTokens` 表示未缓存输入，与 `cacheReadTokens`、`cacheWriteTokens` 不相交，而 OTel 的 input 包含缓存输入。因此 DSH → OTel 转换应在字段已知时计算 `input = inputTokens + cacheReadTokens + cacheWriteTokens`，缺失字段按实际契约判断，不能一概补零；保存其可选权威 `totalTokens` 并核对归一化结果。这与一些供应商直接返回的 prompt_tokens 口径不同。[DSH 固定版本类型定义](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm/src/types.ts)

### 4.4 可靠导出与全请求覆盖

满足“每个请求都观测”需要全量保留请求、模型尝试和工具执行的基础 span，不在入口按概率丢掉成功请求。正文/大结果可按权限和容量单独控制；HTTP 细节也可以另设保留策略。

新增有界本地 outbox、导出确认水位、重试退避和 replay checkpoint。采用至少一次导出与稳定 span ID，实测目标后端重复导入行为，不承诺网络恰好一次。程序关闭时有界 flush；运行中展示 started/finished 事件，因为常规 span exporter 往往在 span 结束后才上报，单靠 OTLP 不一定能显示正在执行的节点。

保留现有审计持久化 fail-closed 规则；远端观测后端不可用则缓冲并告警，不把网络导出失败误记为金融工具执行失败。本地必需审计日志不可写仍应停止新操作。监控 `export_lag`、outbox 容量、缺失开始/结束、孤立 span、unknown usage、采集/导出错误；容量触顶时明确拒绝新执行或标记数据缺口，不能静默丢失却声称全量。

旧日志可以离线回填，但缺少真实边界/TTFT 时保留 null 或标记 reconstructed，不能把事件时间差包装成精确历史耗时。

### 4.5 管理界面与金融业务证据

每个请求显示运行概览、瀑布图、模型轮次、工具调用、token/估算成本、最长耗时、失败步骤和采集完整性；点击工具展开脱敏参数、结果摘要及受控附件。普通用户仅看允许公开的进度，开发者信息通过管理员接口授权。

金融数据额外记录 `data_source`、查询日期范围、数据时间、缓存命中、返回行数、结果摘要哈希与数据新鲜度；这样可以区分“Agent 选错了工具”“数据源过期”和“计算实现错误”。股票等高基数字段放 span 属性，避免放进 Prometheus label；run_id/session_id 也不作为指标 label。

模型输入默认存引用、模板版本/哈希和大小；不复制密钥、完整持仓和无界工具结果。外部后台的保留/删除必须覆盖原生日志、投影、outbox、附件和远端追踪，不能只删除本地 Web 表。

### 4.6 从排障扩展到质量评估

链路稳定后再引入离线评估：时效性工具是否调用、行情是否有日期与来源、参数是否正确、工具失败后是否虚报成功、回答是否引用真实结果、重复调用比例和预算超限。评估关联原 trace；线上回看默认只读，不能重放会修改持仓、预警或订阅的工具。LLM-as-judge 单独记录自身 token/费用，不能混入原用户请求成本。

## 5. 平台组合与选择

推荐默认路径：DSH 薄适配器 → OpenTelemetry/OTLP HTTP → Langfuse；需要统一路由、过滤、多后端时增加 OTel Collector。Langfuse 支持 OTLP 接入，但模型类型、usage、会话关联仍需按照属性映射验证；OTLP 接入成功不等于 token/成本自动正确展示。[官方集成](https://langfuse.com/integrations/native/opentelemetry)

Phoenix + OpenInference 作为偏开放追踪/评估的备选。OpenInference 与 OpenLLMetry 是埋点/语义生态，不等于完整存储后台，也不能默认观测 DSH 子进程。Tempo 用于通用调用链，Prometheus 用于低基数聚合指标，Grafana 用于运维面板；第一期不必同时维护两个 Agent 后台和一整套运维集群。

社区 [CodePrometheus/dsh-observability](https://github.com/CodePrometheus/dsh-observability) 可作为投影实现参考和备选：有 turn/step/tool 投影、OTLP traces 和稳定 ID，但其 0.1.0 版本的 peer dependencies 固定 DSH `0.1.0-rc.6`，与本项目不同。如采用其 projector，需补齐独立模型 attempt、失败 usage、脱敏、持久 outbox 和跨进程关联。它的 step 包含工具执行，不能直接把 step duration 当纯模型耗时。源码为 MIT，移植应保留相应许可声明。[依赖声明](https://github.com/CodePrometheus/dsh-observability/blob/main/package.json)

锁定版本的官方 `session-telemetry-otel` 导出的是 OTel logs，默认 `FEEDBACK_ONLY`，并拒绝 `FULL`；不直接满足全请求实时 trace。官方 `session-telemetry` 提供自定义 backend 接缝，但同 context 单 backend、非阻塞、best effort，需要自行补可靠导出。应避免同时注册冲突 backend。[官方固定版本说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-telemetry-otel/README.md)

社区 [dsh-fast](https://github.com/PerryLink/dsh-fast) 可补充 session 加载、compaction、上下文和 cache 的性能诊断；它不是跨进程追踪后端，也需单独核对版本。第一期优先完成主链路后再考虑引入。

## 6. 建议实施顺序与验收

| 阶段 | 交付 | 验收 |
| --- | --- | --- |
| P0：采集契约和兼容性 | 固定 DSH/plugin/OTel 版本；确认 hook、usage 与上下文可透传字段；最小两轮模型 + 一次工具样例 | 已知调用数量与原生日志一致，模型 token 只计一次；明确插件是否可直接复用 |
| P1：DSH 完整纵向链路 | 实时事件、span 映射、MCP 传播、单后端接入、本地 outbox | 用 run_id 查到全部模型/工具及 MCP 内部步骤；后端断开不破坏已有审计语义 |
| P2：查询和运维 | 管理页接 DSH、费用/覆盖率、慢调用与错误分析、导出补传 | 并发不串链；失败、超时、取消、崩溃、unknown usage 如实显示；回填不重复计费 |
| P3：金融质量评估 | 数据新鲜度、证据关联、评估数据集、提示词/版本对比 | 相同样例能对比正确率、耗时与费用，评估本身成本单列 |

关键测试还包括：同名工具多次调用；工具以 JSON 返回业务错误；流异常无最终 usage；重试后成功；同 input_id 再提交；消费者关闭；DSH/MCP 进程被终止；原生 checkpoint 失败；Exporter 恢复补传；大结果截断标记；敏感值跨 chunk 脱敏。性能使用固定替身负载对比启用前后 p50/p95、本地磁盘写入与队列积压，再测真实供应商；不预先承诺延迟开销比例。

本次仅完成调研与开发方向。后续涉及 Docker 环境、构建或部署时，先读取项目 `agnets.md` 并遵守 Docker Desktop 约束。

## 2026-11-16 补充：优先复用云厂商 DSH 专用插件

前一版未覆盖云厂商维护的 DSH 原生观测插件，现修订采集层优先顺序。平台选择与采集能力分开验证，先复用现成的原生 lifecycle 采集，再补本项目独有边界。

| 使用条件 | 优先 PoC | 已有能力与仍需验证部分 |
| --- | --- | --- |
| 希望通用 OTLP、自选 Langfuse/Phoenix 等后台 | [LoongSuite `@loongsuite/dsh-plugin`](https://github.com/loongsuite/dsh-plugin) | 已有 ENTRY/AGENT/STEP/LLM/TOOL、每 attempt span、usage 和耗时 metrics、错误/取消收尾；正文默认关闭。先验证与 pinned runtime 兼容，然后补宿主/MCP/业务 I/O |
| 已有腾讯云 CLS | [`tencentcloud-agentobs-sdk-dsh`](https://cloud.tencent.com/document/product/614/136048) | 官方 DSH 接入支持父子链、耗时、TTFT、token、失败重试和多轮会话；先核对版本，再复用 AgentObs 展示与运维能力 |
| 已有火山引擎 TLS | [TLS DSH 观测](https://docs.volcengine.com/docs/torch-log-service/deepseek-harness-observability?lang=zh) | 官方 DSH 应用、专用安装器及 trace/token/TTFC 字段；先核对插件版本、采集口径和内容开关 |

LoongSuite 独立插件输出标准 OTLP/HTTP protobuf，不依赖 Pilot、sidecar 或特定云；[官方实践文章](https://www.alibabacloud.com/blog/deepseek-harness-a-practical-guide-to-comprehensive-observability_603771) 同时介绍独立插件与 Pilot。Pilot 更适合统一采集多种 Coding Agent；[云监控接入说明](https://help.aliyun.com/en/cms/cloudmonitor-2-0/ai-application-access-ai-coding-agent/) 列出自动发现、部署 hook/plugin、token 与 trace 汇聚能力。当前项目采用 SDK 内嵌 runtime，优先试独立插件对实际 `sdk-minimal` + finance patch 的挂载。

必须保留版本门槛：此次核对的 LoongSuite README 和腾讯云官方文档均声明 DSH `>=0.1.0-rc.6 <0.2.0`，不包含本项目 `0.2.1rc2`。火山方案未核实本项目 pin 支持范围。因此“优先 PoC”指优先做版本适配与真实 composition 验收，并非可直接投入生产或要求降级运行时。

P0 的具体交付改为插件兼容矩阵和最小垂直样例：LoongSuite 通用 OTLP 首选，既有云平台则换对应官方插件；只有缺失的采集才自研。验收继续覆盖每请求 trace、独立 attempt、token 去重、stdio MCP 传播与业务内部 span。云厂商插件能够观察 DSH 工具调用，并不等于自动追踪 Python MCP 服务内部的 HTTP/计算过程；本地 outbox、重放去重、checkpoint 关联和采集完整率也仍需实测。不要同时启用多套为同一 LLM/tool 生成 span 的插件。

本次补充只修改选型文档，未安装、部署或发送任何遥测数据。

## 2026-11-16 实施：LoongSuite Linux 容器接入

后续实现已完成第一阶段，接入说明见 [DSH 观测开发说明](../OBSERVABILITY.md)。新增独立 `Dockerfile.observability`、开发 Compose、固定 npm 锁文件、环境配置和 V4 工具结果适配；实际挂载在项目 `sdk-minimal` 的 finance profile。现有业务服务与业务卷未切换。

真实 Linux Docker Desktop 容器验证组合：SDK/runtime `0.2.1rc2` + `@loongsuite/dsh-plugin@0.1.3` + 本项目兼容适配器。两轮本地模型调用、一次真实 MCP 工具执行形成 7 个 span，父子关系与单 trace 正确；正常工具及业务失败工具分别校验。验证 input 10/output 2/cache-read 4 的每调用用量、首片段字段、run_id、鉴权 header、内容关闭和退出 flush。配置、Harness 执行/恢复、trace storage 与 runtime selection 共 19 项测试通过。

端到端共 3 项通过，另覆盖 OTLP 503 故障不影响原生持久化完成状态。上游 DSH SDK 关闭时有 TextIOWrapper ResourceWarning，保留在验证输出中，未修改第三方 SDK。阿里云 endpoint/License Key 尚未提供，因此未向真实云账号上报。后续范围包括 MCP Python 内部 spans、辅助模型调用、持久 outbox 和云端验收；不把本次适配解释成所有 DSH 0.2 场景已覆盖。

## 2026-11-17 本地调试交付

用户明确本阶段不需要公网 OTLP，调整为 LoongSuite → Docker 网络内的 Jaeger 2.23.0。独立开发 Compose 不再依赖私有 env 文件，UI 仅发布到 127.0.0.1:16686，OTLP 不发布到宿主网络，默认不导出 metrics、不采集正文。真实模型调试使用独立开发卷，现有 Web/LangGraph 服务不变。

已启动本地 Jaeger 并验证 UI HTTP 200。`tests/local_observability_smoke.py` 使用本地模型替身 + 真实 DSH/MCP，随后通过 Jaeger v3 API 核对 7 个 span、父子关系、2 次模型调用各 input=10/output=2，以及 1 次成功工具调用。结果 passed，trace ID `a664026e7b4f867011731f98a585799f`，run ID `run-7f34b01ebc714ffab7220f20c11204e1`。数据为当前 Jaeger 进程内存数据，重启后可运行 smoke 重新生成。

操作说明以 [本地观测文档](../OBSERVABILITY.md) 为准。此前云端配置步骤仅保留为未来可选参考，不再是本任务的未完成项。
