# DeepSeek Harness 接入与逐提问轨迹回溯规格

状态：Ready for implementation（待实现；本文不表示已完成接入）  
规格日期：2026-11-02  
目标分支：dev-v1.1.0  
基线：合并提交 9234291；实现前重新核对工作区和容器版本。

## Problem Statement

用户需要在回答异常、行情错误、工具超时或 Agent 中断时，按一次提问还原发生了什么：采用了什么模型与配置、进行了哪些模型请求、为什么进入某个工具步骤、传入和返回了什么、何时失败，以及最终回答使用了哪些证据。

当前源码由 LangGraph 编排，使用 LangChain ChatOpenAI 调用 DeepSeek，金融工具由统一分发器执行。内存历史包含工具调用和关联结果，但 CLI 主要在手动保存或退出时保存会话。对外事件没有完整的运行身份和逐调用时间，多个工具集中在同一个图节点内执行，无法保证异常退出后仍能回溯。

先前核验的运行容器仍使用旧版 fin-agent:local；新版源码或镜像存在不等于运行环境已经切换。当前没有 DeepSeek Harness 集成。配置 DeepSeek API Key 与接入 Harness 是两件事。

## Solution

采用官方 DeepSeek Harness Python SDK 管理实际 Harness 运行时，由 Harness 成为每次提问的唯一 Agent 循环执行者。通过本地 MCP 工具桥复用已有 Python 金融工具，不重写行情、回测、组合和新闻业务逻辑。现有 CLI 和流式调用接口保留，通过适配层转换事件。

每次被接受的提问获得可复制的 run_id；即使失败或取消，也能查询状态。Harness 原生持久化会话记录是执行事实的主记录，应用补充接收记录、关联索引和可查询投影。用户可以列出运行、查看按时间排列的模型与工具步骤、查看错误及脱敏参数结果，并导出诊断包。回溯默认只读取记录，不重新执行模型请求、行情请求或组合修改。

正式交付必须证明真实 Harness 驱动模型和工具、记录在重启后可恢复、当前 Docker 部署实际启用了新运行时。仅新增日志包装器、安装 SDK、启用 LangGraph 回调，或者导出对话 JSON，都不满足本规格。

## User Stories

1. As an 使用者, I want 每次提问得到唯一运行编号, so that 我能准确报告某一次问题。
2. As an 使用者, I want 查看提问、回答和终止原因, so that 我知道请求是完成、失败、取消还是中断。
3. As an 使用者, I want 查看该提问的所有工具调用顺序, so that 我知道回答依据来自哪里。
4. As an 使用者, I want 查看每个工具的输入和完整可用输出, so that 我能核对股票、日期、数量等参数。
5. As an 使用者, I want 区分同名工具的不同调用, so that 多轮查询不会混在一起。
6. As an 排障人员, I want 查看模型请求和工具调用的父子关联, so that 我能还原 Agent 的执行过程。
7. As an 排障人员, I want 查看耗时、超时和错误类别, so that 我能定位模型、网络或业务工具问题。
8. As an 排障人员, I want 查看模型尝试和重试记录, so that 一次失败不会被后续成功掩盖。
9. As an 排障人员, I want 查看当时的模型、数据源、提示词和工具版本, so that 当前配置变化不会误导分析。
10. As an 排障人员, I want 查看模型实际返回的推理字段和用量（如果有）, so that 我能理解可观测的模型行为及成本。
11. As an 使用者, I want 程序崩溃或容器重启后仍能查询已提交轨迹, so that 关键错误不会随内存丢失。
12. As an 使用者, I want 明确看到没有执行、执行失败与执行结果未知的区别, so that 我不会误判组合或预警已经修改。
13. As an 使用者, I want 在一段会话内连续提问并按问题分别查看轨迹, so that 上下文连续且问题边界清楚。
14. As an 使用者, I want 清空当前聊天不删除历史诊断记录, so that 我仍可追查之前的问题。
15. As an 使用者, I want 同时打开不同会话而不串记录, so that 查询和组合操作相互隔离。
16. As an 运维人员, I want 存储不可写时得到明确错误, so that 系统不会悄悄执行无法审计的工具。
17. As an 使用者, I want 导出的诊断包隐藏密钥, so that 我能安全分享排障材料。
18. As an 使用者, I want 原有金融工具和 CLI 使用方式继续可用, so that 更换 Agent 框架不会丢失业务能力。
19. As an 运维人员, I want 独立镜像验证和可回滚部署, so that 升级不损坏已有配置及持仓。
20. As an 排障人员, I want 离线查看旧运行而不调用工具, so that 回溯不会产生新的费用或副作用。
21. As an 排障人员, I want 分页查询和完整导出较长轨迹, so that 大结果和长会话也可排查。
22. As an 运维人员, I want 看到实际运行的框架及版本, so that 我能发现源码已更新但容器仍运行旧版的问题。
23. As an 使用者, I want 追踪后台任务与创建它的提问的关系, so that 我能找出预警或新闻订阅最初来自哪次操作。

## Implementation Decisions

### 1. 接入方式与责任边界

- 保留 FinAgent 作为应用入口；内部新增 Harness Runtime Adapter，负责生命周期、会话映射、取消、流式事件和故障映射。切换后同一请求不得同时运行 LangGraph 和 Harness。
- 首选官方 deepseek-harness-sdk 及其严格匹配的 runtime wheel，通过 SDK 的 stdio JSON-RPC 启动子进程。[官方 SDK](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/python/sdk/README.md) 已描述该方式及显式 Harness home 要求。
- 使用经过裁剪和锁定的 sdk-minimal profile：保留模型、会话、持久化及 MCP，禁用默认 shell、文件浏览、图片工具及动态插件管理；只注册项目所需金融工具。官方最小 profile 默认仍带 shell，不能直接将其全部能力暴露给金融 Agent。[profile 说明](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/bundle/sdk-minimal/README.md)
- MCP 工具桥采用本地 stdio 子进程，不开放主机网络端口。统一导出已有工具定义并调用现有分发器；不得把全部金融工具包成一个无法区分内部调用的通用工具。
- 每个业务工具在 Harness 中对应一个可见工具身份。若 MCP 前缀改名，保存原始名称与暴露名称的双向映射，更新提示词引用并校验无重名。
- 桥接协议只在 stdout 输出协议数据；普通日志写 stderr。已有交互式配置工具不得在桥中等待 input；返回结构化 configuration_required，配置操作由宿主 CLI 完成，随后记录非敏感配置版本变化。
- 注册工具时区分只读行情/分析与写入组合、订阅、预警、配置的工具。初版按当前语义串行执行；不得因框架默认并发而改变业务行为。
- 保留现有 Session Store 的会话列表和 UI 元信息；新运行的模型上下文以 Harness 持久化记录为准，不建立两个可以独立修改的历史主记录。旧会话只读可查；继续旧会话时生成有明确 imported_context 标记的新 Harness 会话，不伪造历史执行轨迹。

### 2. 版本与前置兼容性门禁

- 官方文档的 master 是调研依据，不是可部署锁文件。实施阶段先锁定 SDK、runtime、profile/plugin 和 MCP 适配依赖的精确版本及来源校验值，记录对应上游 commit 或 release。
- 在与当前 Docker 相同的 Linux/Python 平台验证 wheel 可安装、运行时可启动、profile patch 可加载、MCP 工具能发现并执行、会话可持久化及恢复。优先发布 wheel；若没有兼容发布，则从固定官方 commit 构建并记录产物校验值。不得静默退化为自制“类 Harness”实现。
- 保留当前 DeepSeek 模型设置；当前配置值 deepseek-chat 必须通过所选版本的路由兼容性测试。若官方适配器不支持该名称，应配置其公开支持的兼容路由，不得未经说明自动改成文档示例中的其他模型。
- 保存运行版本元信息：应用 commit、镜像 digest、Harness/SDK 版本、profile hash、tool catalog hash、数据源及模型标识。模型密钥仅供凭据通道使用，不进入版本快照。
- 若某版本没有公开的最终持久化确认接口，需通过受支持的 profile 插件补上 flush/ack；不能把 SDK 返回最终文本误当作已经持久化。此门禁未通过时不得进入部署阶段。

### 3. 身份与提问边界

- application_session_id 表示用户会话；harness_session_id 表示真实框架会话；run_id 表示一次宿主接收的用户提问；input_id 表示请求身份；turn、step、attempt_id 使用原生身份并建立映射。
- tool_execution_id 是项目范围唯一的执行编号；同时保存模型 tool_call_id、Harness 工具身份、父 attempt_id、run_id 和调用次序。重试使用新的 attempt/execution 编号，关联 retry_of；不复用一次执行的开始和结束身份。
- 每个会话同时仅允许一个活跃提问，后续提问排队；不同会话可并行。initial receipt、运行关联和排队状态先写入应用接收日志，再向 Harness 提交。
- 按 input_id 与原生 turn 事实关联，不通过“某时间段内所有通知”推断提问范围。官方 SDK 的通知区间可能包含重叠活动，单独等待全 Agent idle 也不是可靠的每提问边界。[Agent 生命周期](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/core.md)
- 同一 input_id 重复提交返回原运行状态；传输超时不是执行失败的证明，先查询事实，禁止自动重新发起有副作用的操作。
- 应用 run 状态至少有 queued、running、succeeded、failed、cancelled、interrupted、rejected。tool 状态另含 requested、started、succeeded、failed、cancelled、skipped、outcome_unknown。只能追加状态事实，不覆盖原历史。

### 4. 记录内容与持久化语义

- 配置原生持久化后端及 checkpoint policy，显式 Harness home 位于既有持久化数据卷的新命名空间内。记录数据不写入镜像层或源码仓库。官方 session 包默认内存存储，必须另外装配后端。[会话存储说明](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/core/session/README.md)
- 原生事件日志保存模型可见执行事实；项目补充 append-only 接收/关联记录。SQLite 仅作查询投影，可从原始日志重建，不作为丢失原生记录后的替代品。日志和投影建立已确认序号 watermark，查询显示完整性与同步状态。
- 原生 turn/start、turn/end、step/start、step/end、request/header、assistant/message、assistant/attempt、tool/call、tool/result 等按锁定版本公开事件契约读取。通过适配器映射为稳定的应用事件，不向上层泄露不稳定上游字段。未知事件保留原始类型并明确标记，不静默丢弃。[事件定义](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/session.md)
- 每个模型尝试记录路由、模型、开始/结束、耗时、请求配置、提示词/历史引用、工具 schema 快照、返回文本、公开返回的 reasoning 字段、usage、finish reason 和错误。不可获得字段为 null，并说明 unavailable；不推断或编造隐藏思维链。
- 每个工具执行记录参数、规范结果、模型实际看到的结果、数据源、开始/结束、耗时、结构化错误、取消/超时、重试关系；日志中不能仅保留 CLI 截断的前 200 个字符。保留 API 实际提供的请求 ID，缺失则为空。
- 先确认接收记录落盘，再确认模型请求和工具执行的开始事实落盘，之后才允许执行。工具结果与运行终止事实必须确认持久化后，才向宿主宣布相应完成。错误路径同样需要终止记录。
- 原生默认 checkpoint 不保证最后一次响应已落盘；应用 profile 必须在工具结束、运行结束及正常关闭执行持久化屏障。数据卷无法写入、容量不足或 flush 失败时，拒绝启动新模型/工具操作并暴露 storage_error，不继续无记录执行。
- 崩溃发生在工具完成但结果尚未提交之间时，只能标记 outcome_unknown，不能宣称执行成功或“恰好一次”。写入工具不得自动重试；需要核对业务持久化状态。业务桥记录 run_id/execution_id，能提供幂等键的写操作应使用幂等键。
- 模型流片段允许实时推送；已结算模型尝试必须持久化。硬崩溃前尚未提交的 token 流可能缺失，查询必须展示缺口。原生文档也说明未结算尝试在硬崩溃时不能保证完整保存；本规格不承诺零丢失流式 token。
- 大型输出使用持久化、带内容摘要和字节数的附件，事件保留引用及预览。附件必须在引用事件完成前提交。不得只截断却不标记，也不得在引用不可读时声称轨迹完整。
- UTC 时间用于跨进程展示，单调时钟用于进程内耗时；跨进程顺序以持久化序号和因果引用为准，不以机器时钟强行排序。
- 默认保留全部记录，不自动删历史；提供容量统计、可配置硬上限和显式清理入口。清理只能对已终止、未被恢复任务占用的整个会话及其附件进行，先导出并明确清理范围，不破坏活动上下文。

### 5. 查询、导出与兼容接口

- 保留 stream_chat 和 run 的现有事件类型与返回行为；增量附加 run_id、session_id、step_id、tool_execution_id、tool_call_id、event_seq、timestamp、status。旧消费者忽略新字段仍可使用。
- 提问被接受后立即展示 run_id；最终回答、错误和取消输出再次提供该编号。run 完成以其持久化终止事实为准，而非 stdout 停止或 idle 状态。
- 新增 trace list、trace show、trace export 三类 CLI 能力，支持会话、运行、状态、工具、时间范围筛选和基于序号的稳定分页。具体命令拼写在 CLI 实现中固定并写入帮助。
- trace show 至少展示提问、运行元信息、模型尝试、逐工具输入输出与耗时、错误、最终回答和完整性告警。默认隐藏冗长正文，支持展开；不得隐藏失败步骤。
- 导出为版本化 JSON 诊断包，包含清单、关联图、事件、附件摘要及可选附件正文。导出可在无网络、无模型密钥时完成。导入后的只读查看不得触发模型或工具执行。
- 回溯指历史事实重建；“重新执行”是新的 run，需显式操作并记录来源 run_id。首版不提供自动重放写入工具。
- Harness 的 SDK 子进程不天然提供 Web UI；首版验收使用 CLI/JSON 查询。完整 Web 轨迹页面不是交付前置，也不能把未启用的上游 Web 页面当成已有产品功能。
- 保持现有 `/save`、`/load`、`/clear` 语义：save 确认当前记录持久化；load 恢复相应 Harness 会话；clear 创建新上下文并保留旧轨迹。旧数据格式导入要标记历史来源和可观测能力限制。
- 由工具创建的订阅、预警记录 originating_run_id 和 tool_execution_id。后续定时触发以后台任务身份记录关联，不能冒充该提问中仍在运行的工具调用；首版不将整个后台调度系统迁入 Harness。

### 6. 凭据和敏感信息

- DeepSeek API Key、Tushare Token、SMTP 密码、Authorization/Cookie 等永不进入模型消息、工具参数、原生日志、错误堆栈导出或查询索引。所有通道在持久化前脱敏，不能只在 UI 展示时掩码。
- 对输入/输出进行字段和已知秘密值检测，保留脱敏标记；必须覆盖 SDK stderr、上游异常文本、桥接错误和附件。金融数据与用户提示默认仅保存在本机数据卷，导出清楚标注其内容类别。
- 配置凭据通过宿主可信界面处理；不能通过让模型回显新密钥完成配置。此前会话中出现过的真实密钥不得复制到本规格、测试或示例。
- 诊断读取为只读能力。初版按本地单用户部署，不引入公共追踪服务或默认云端遥测。

### 7. Docker 部署、迁移与回滚

- 维持 Docker Desktop Linux 引擎、非 root 用户和现有持久化业务卷。以独立新标签构建 Harness 镜像，在隔离测试卷验证，不覆盖当前运行镜像或删除业务卷。
- 构建时安装锁定 SDK/runtime 与必要 profile 资源；容器启动不得临时下载依赖或执行动态安装。初始化 profile 必须幂等，重启保留历史。
- 部署前备份现有配置和业务数据；新增追踪使用独立命名空间，旧版程序无需解析其格式。首次迁移为非破坏性新增，原始会话文件保留。
- 提供显式 engine 选择和启动状态输出：应用版本、实际引擎、Harness 版本、持久化状态。新部署默认使用 Harness；旧 LangGraph 仅作为显式回滚选项，不在故障时静默切换。
- 关闭时停止接收新请求、取消或等待当前执行、提交终止记录并 flush，再关闭 MCP 和 Harness 子进程。强制中断则由下次恢复记录 interrupted/outcome_unknown。
- 回滚恢复旧镜像和旧引擎，业务配置继续可用；已产生的 Harness 轨迹保留，并用独立查询能力读取。不能将新版会话覆盖成旧版 JSON 后冒充兼容迁移。

### 8. 交付顺序

1. 兼容性验证：锁版本、启动真实 Harness、注册一个真实桥接工具、验证原生持久化及最终 flush，形成可重现的构建证据。
2. 实现单一 Runtime Adapter 与金融 MCP 桥，保留业务接口，接入现有工具清单。
3. 实现身份关联、存储屏障、故障恢复和脱敏；此时才允许默认启用 Harness。
4. 实现查询、导出、旧会话导入和运行状态展示。
5. 执行下述验收、隔离镜像验证与部署回滚演练，再更新当前 Compose 部署。

## Testing Decisions

主要测试边界只有一个：从应用提问入口提交请求，再从公开轨迹查询接口验证结果。使用真实 Harness runtime、真实 MCP 桥和真实持久化后端，模型 HTTP 服务和金融数据供应商使用可控替身。测试观察回答、业务副作用和落盘轨迹，而不是验证内部函数调用次数。

现有测试已有脚本化模型、会话 round-trip、工具错误反馈和回测数据源替身，可复用这些场景与数据；不能只沿用绕过真实 Harness 的 Fake LLM 测试就宣称完成接入。工具 schema 转换和脱敏器仅补充必要的小范围单元测试。

验收清单：

1. 一次无工具提问：有唯一 run、输入、模型尝试、最终回答和持久化终止记录。
2. 一次提问包含两个同名不同参数工具和后续模型总结：每个工具拥有不同执行 ID，参数、结果、父模型尝试及顺序可准确关联。
3. 真实 Harness 工具注册表包含金融工具；模型选择该工具后经 MCP 桥执行现有业务函数。禁用桥后调用明确失败，不能走未追踪的备用执行路径。
4. 模型重试、工具业务失败、非法 JSON 参数、网络错误和超时各自留有可区分的状态与错误信息，最终结果不覆盖早期失败。
5. 同一会话连续两个问题、两个会话并行及重复 input_id：运行边界明确，没有通知串线或重复副作用。
6. 在请求接受后、工具开始前、工具执行中、工具完成但结果未提交时分别强杀进程：恢复后保留已提交事实，未完成项被标记，写操作不会自动重复。
7. 在成功响应返回后立即强杀并重启：该运行全部已确认工具结果和最终状态仍可查询。验证此项时不能依赖正常退出触发 flush。
8. 模拟磁盘满、不可写和 flush 失败：没有任何未经开始事实持久化的模型/工具执行，用户获得可诊断错误。
9. 流式连接断开及 SDK 等待超时：不直接宣称业务失败，不盲目重试；重新连接可查询同一 run 的实际状态。
10. 注入测试专用假密钥到配置、工具错误和日志文本：扫描原生文件、索引、附件、stderr 和导出，假密钥原文均不得出现。
11. 大工具结果跨越预览限制：诊断包可读取完整保留内容且摘要一致；缺失附件明确报不完整。
12. 删除可重建查询索引后重建：运行数量、状态、因果引用和工具结果一致；未知原生事件不被静默丢弃。
13. 查询及导出期间将模型服务和工具端点设为拒绝访问：查询仍完成且不触发任何业务副作用。
14. 原有 AkShare 行情、技术指标、回测、多组合及订阅工具通过桥保持输入输出语义；写操作失败与成功状态准确识别，已有返回字符串错误不得被桥误标为成功。
15. `/save`、`/load`、`/clear`、旧会话导入和正常/异常退出的兼容性通过；历史记录不因 clear 被删除。
16. 达到步骤上限、总时间预算或取消时，运行可终止并记录 skipped/cancelled；工具已有副作用但完成性未知时保留 outcome_unknown。
17. Docker 重启后配置、持仓及轨迹均保留；回滚旧引擎后业务可启动；新运行时的实际版本与镜像标识可核查。
18. 以每次 10 个工具调用、每个结果 10 KiB 的固定本地场景测量追踪开销，公布机器环境和基线；目标每个持久化边界 p95 附加延迟不超过 100 ms。失败时优化或明确阻断发布，不能取消持久化保证以通过指标。
19. 使用真实 DeepSeek 的小额 smoke 请求验证模型路由及一次只读工具调用；只记录脱敏结果和用量。离线测试不得代替这一部署验证，但无凭据的 CI 不运行该项。

完成门禁：上述必需行为通过、真实运行时身份和持久化证据可复现、当前部署确已切换、回滚验证成功，才可宣布功能已完成。编写本规格不执行这些部署动作。

## Out of Scope

- 本次规格交付不直接修改 Agent 代码、运行容器、凭据或推送 GitHub issue；后续实施按本规格推进。
- 不重写金融算法，不移除 AkShare/Tushare provider，不改变模拟持仓业务含义。
- 不承诺模型结果可确定性复现、不获取模型未公开的内部思维链、不自动重放写工具。
- 不建设多租户云端监控、公开 HTTP 查询服务或完整 Web 控制台。
- 不对操作系统硬故障下尚未提交的 token 片段作零丢失承诺；必须明确展示已知缺口。
- 不将独立后台定时任务全部改写为 Harness Agent，只保留其创建来源关联。

## Further Notes

用户明确指定将规格写入项目文档，并授权采用推荐方案，因此覆盖 to-spec 技能默认的 issue tracker 发布和测试边界确认流程。本规格在本地交付，不要求额外采访或插件配置。

本文件中的架构、默认行为、数据字段和验收要求是本项目的设计决定；不是声称 SDK 已原样提供所有能力。尤其是最终持久化确认、金融工具桥、应用查询投影、脱敏和副作用恢复，需要实现并验证。

上游接口可能变化；实施前必须复核固定版本公开契约。调研资料访问日期为 2026-11-02，正文已就关键事实就近引用，另可参阅 [runtime wheel 说明](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/python/sdk-runtime/README.md) 与 [工具执行契约](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/tools.md)。不得依赖上游明确标记禁止用于新生产调用的内部或废弃读取接口。
