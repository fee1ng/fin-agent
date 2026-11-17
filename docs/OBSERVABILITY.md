# DSH + LoongSuite：Linux 容器本地观测

本次接入直接运行在 Python SDK 启动的 DSH `sdk-minimal` 内，独立于 Web 的 LangGraph 观测。无需 LoongSuite Pilot，也不需要在宿主机运行 Node 服务。

## 本地调试（默认路径）

按用户要求，当前使用 **DSH → LoongSuite → 本地 Jaeger**，不需要阿里云账号、公网 OTLP 地址或 License Key。`compose.observability.yaml` 固定使用 Docker 内网 `http://jaeger:4318`，并清空共享及 trace 专用鉴权头，避免私有 env 文件中的云配置影响本地调试。

从仓库根目录的 Windows PowerShell 执行（Docker 命令入口遵守 `agnets.md`，实际服务运行在 Linux 容器）：

```powershell
$dockerCli = 'C:/Users/93120/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe'
& $dockerCli context inspect desktop-linux
& $dockerCli --context desktop-linux compose -f compose.observability.yaml up -d jaeger
& $dockerCli --context desktop-linux compose -f compose.observability.yaml build harness-observability
& $dockerCli --context desktop-linux compose -f compose.observability.yaml run --rm smoke
```

打开 <http://localhost:16686>，选择服务 `fin-agent-local` 并搜索 Trace。展开模型 span 的 Tags 查看 `gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`、缓存 token 和首片段字段；瀑布图显示执行顺序、父子关系及耗时。Jaeger 是通用 Trace 界面，token 以属性展示，不提供专用 LLM 成本面板。

`smoke` 使用本地模型替身、真实 DSH 和真实 MCP 时间工具，生成两轮模型调用及一次工具执行；随后查询 Jaeger API 校验 7 个 span 和用量，并打印具体 trace URL。不需要真实模型密钥，不调用云模型或行情服务。构建和首次拉取镜像需要网络，运行中的遥测仅发往本地容器。

调试实际问题时执行：

```powershell
& $dockerCli --context desktop-linux compose -f compose.observability.yaml run --rm harness-observability
```

真实模型凭据沿用 CLI 初始化，也可放入可选的 `.local/observability.env`；真实模型自身的网络调用与“遥测仅本地”是两回事。当前已运行的 Web/LangGraph 服务不会自动进入 DSH 链路。

Jaeger 只将 UI 端口绑定到 `127.0.0.1`；OTLP 不发布宿主端口。使用官方 Jaeger 2.23.0 默认 all-in-one 内存存储，重启 Jaeger 后详细链路会消失，适用于本地调试。原生 Harness 日志使用独立开发卷保留。停止调试环境使用 `compose -f compose.observability.yaml stop`，不要使用 `down -v` 删除数据卷。[Jaeger 官方说明](https://www.jaegertracing.io/docs/2.23/getting-started/)

## 版本与已实现范围

- SDK/runtime：`0.2.1rc2`，沿用项目固定版本。
- LoongSuite：`@loongsuite/dsh-plugin@0.1.3`，npm 完整传递依赖与 integrity 由 `deploy/observability/package-lock.json` 固定；构建使用 `npm ci --ignore-scripts`。
- 上游声明仅支持 DSH `<0.2.0`。本项目通过 `harness_observability.mjs` 将 **观测副本** 中的 V4 工具结果转为插件支持的格式，不降级 DSH、不改写原生事件。该组合属于本项目验证的适配，不代表上游官方承诺支持全部 0.2 功能。
- 每次请求导出 ENTRY → AGENT → STEP → LLM/TOOL；按真实调用记录模型尝试、工具结果、耗时、首片段时间、输入/输出/缓存 token。
- 所有资源带 `fin_agent.run_id`，可与 CLI 的 run_id、原生 receipts 对照。一个 runtime 服务一次请求，因此该字段随请求切换。
- 显式关闭提示词、回答、工具参数/结果正文采集，环境变量不能绕过。阿里云鉴权只从环境变量读取，不写入生成的 profile。
- 原生 checkpoint/flush 仍决定运行完成。遥测导出失败不会把已完成的业务请求改成失败。

## 可选：以后切换云端时的配置参考

以下不是本地调试的前置步骤。当前 Compose 会强制使用本地 Jaeger；未来切换云端需另建明确的云端部署配置，不能只填写 env 文件就认为已切换。

在阿里云云监控 2.0 的 AI 应用/接入入口创建开发用途的应用和工作空间，选择 **公网、HTTP/OTLP**。以控制台给出的端点和鉴权字段为准，不使用 VPC 内网地址。

将 `deploy/observability/aliyun.env.example` 复制到已被 Git 忽略的 `.local/observability.env`，填写：

```dotenv
FIN_AGENT_OBSERVABILITY=true
OTEL_SERVICE_NAME=fin-agent-local
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=development
OTEL_EXPORTER_OTLP_ENDPOINT=https://实际项目.实际地域.log.aliyuncs.com/apm/trace/opentelemetry
OTEL_EXPORTER_OTLP_HEADERS=x-arms-license-key=实际值,x-arms-project=实际值,x-cms-workspace=实际值
OTEL_METRICS_EXPORTER=none
```

共享 endpoint 使用 OTLP 基地址，插件追加 `/v1/traces`。如果控制台直接提供完整 trace 地址，改用 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`，不要重复追加。header/resource 是逗号分隔的 `key=value`，值内特殊字符使用百分号编码。

token 已记录在 trace 中，不要求额外开启 metrics。若需要指标导出，设置 `OTEL_METRICS_EXPORTER=otlp` 并配置云端支持的 metric 地址；当前 resource 包含 run_id，应先在 Collector 中移除 metrics 上的 run_id、service.instance.id 等高基数字段并重新聚合，或制定相应容量策略。

本地模型配置沿用项目 CLI 配置方式；也可将真实模型所需环境变量加入私有 env 文件。不要把密钥提交到仓库或复制进命令输出。

## 构建和运行

Docker 管理入口遵守 `agnets.md`：本仓库由 Windows PowerShell 调用 Docker Desktop 的 docker.exe，实际 Agent 与插件都运行在 Linux 容器内。不通过 WSL 调用 Docker。

```powershell
$dockerCli = 'C:/Users/93120/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe'
& $dockerCli context inspect desktop-linux
& $dockerCli --context desktop-linux compose -f compose.observability.yaml build
& $dockerCli --context desktop-linux compose -f compose.observability.yaml run --rm harness-observability
```

`Dockerfile.observability` 默认基于已存在的 `fin-agent:harness-latest` 镜像；全新机器先按现有 Harness 构建说明准备该基线镜像。镜像中固定安装插件到 `/opt/fin-agent-observability`，DSH 按绝对路径加载本项目适配器，不依赖 `dsh plugin --profile web add` 对另一个 profile 的修改。

Standalone Compose 使用独立开发卷 `observability-dev-data`，不会复用生产持仓/会话卷。也不会修改当前运行的 Web/API/Worker 容器。基础镜像默认关闭观测，本地 Compose 显式开启；临时关闭可在 `compose run` 中增加 `-e FIN_AGENT_OBSERVABILITY=false`。

从 Linux 容器看，`127.0.0.1` 是容器自身；连接宿主机上的开发 Collector 使用 Docker Desktop 的 `host.docker.internal`。云端使用公网 HTTPS endpoint，无需给本地 Agent 开放入站端口。

## 云端验收

1. 提问一个需要调用当前时间工具的问题，复制 CLI 返回的 run_id。
2. 在云端按 service.name `fin-agent-local` 和 resource 字段 `fin_agent.run_id` 定位 trace。界面未直接提供筛选时展开 Resource 属性检索。
3. 确认同一 trace 包含各 step、模型调用和工具；核对调用数量、工具状态、耗时及 token。模型父级上的聚合 usage 不与模型 span 的 usage 再次累加。
4. 输入/输出正文应为空；这是默认数据策略。不要以缺少正文误判为未接入。
5. 重复读取同一 input_id 的结果不应新增模型调用和费用；用户主动再次提问则是新 run。

## 自动验证

`tests/observability_probe.py` 在本地同时启动模型 SSE 替身和 OTLP/HTTP protobuf 接收端，运行真实 Harness、MCP 和插件。仅测试工具 `get_current_time` 与缺少必要参数的 `get_daily_price`，不访问真实模型或行情接口。

在观测镜像中安装测试专用依赖 `opentelemetry-proto==1.40.0` 到临时目录即可执行。下面命令仍从 Windows PowerShell 发出，sh/python 在 Linux 测试容器内运行：

```powershell
& $dockerCli --context desktop-linux run --rm `
  --mount "type=bind,source=$($PWD.Path)/tests,target=/app/tests,readonly" `
  --entrypoint sh fin-agent:harness-observability -c `
  'pip install --target /tmp/probe-deps opentelemetry-proto==1.40.0 && PYTHONPATH=/tmp/probe-deps:/app python -m tests.observability_probe'
```

检查内容包括父子 span、run_id、两轮 usage、缓存 token 不重复累加、首片段时间、业务错误、鉴权 header、正文关闭、幂等回读、退出时 trace/metrics flush，以及远端 503 时原生完成状态保持正确。配置单测与已有 Harness/恢复/存储测试一并运行；证据见 [开发路线记录](plan/agent-observability-roadmap.md)。

## 当前边界

- 当前目标为本地 Jaeger 调试，云控制台验收不属于本阶段要求。
- 目前观测到 DSH 的 MCP 工具调用整体，没有自动贯通 Python MCP 内部 HTTP/数据库/计算 span；Web 的 LangGraph 请求也不经过此插件。
- 上游插件忽略带 purpose 的辅助模型调用（如 title/compaction），本阶段不承诺覆盖这些费用。用户请求中的主 Agent 模型和工具链已验证。
- 插件是内存有界队列、best-effort 导出；进程硬杀、长时间网络故障可能丢遥测，没有新增持久 outbox 或历史回填。原生审计事实仍保留，可离线查询。
- 模型错误字符串和工具名称等元数据仍可能进入追踪；关闭正文不是对任意异常文本的完全脱敏保证。
- 原 SDK 在关闭测试进程时会产生已观察到的 `ResourceWarning`（未关闭 TextIOWrapper）；测试结果不受影响，未借本次任务修改第三方 SDK。

官方参考：[LoongSuite 插件](https://github.com/loongsuite/dsh-plugin)、[阿里云 DSH 接入实践](https://www.alibabacloud.com/blog/deepseek-harness-a-practical-guide-to-comprehensive-observability_603771)。
