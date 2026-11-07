# Web 容器部署与恢复

本轮交付仅需本机连接：保留默认回环绑定 `127.0.0.1:8443`，不开放内网或外网。下文真实内网入口与机构证书配置为备用操作说明，不属于本轮待办。

执行前读取 [Docker Desktop 约束](../agnets.md)。所有示例从项目根目录的 Windows PowerShell 执行。以下变量仅简化命令，不调用 WSL。

```powershell
$docker = 'C:\Users\93120\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe'
& $docker --context desktop-linux context inspect desktop-linux
& $docker --context desktop-linux info --format '{{.OperatingSystem}}'
```

应确认 Docker Desktop、Linux 引擎。默认 Compose 项目 `fin-agent-web` 创建独立 `fin-agent-web_web-data` 卷；与已有 Harness 容器和 `fin-agent-harness-data` 卷无关。禁止运行 `down -v`、卷清理、Desktop 重置或后端切换。

## 配置及启动

在 Git 忽略的 `.local/web-service.env` 中提供服务端 LLM_PROVIDER、DEEPSEEK_API_KEY/BASE_URL/MODEL 或 OPENAI_API_KEY/BASE_URL/MODEL。仅 Worker 接收模型环境；API 和静态资源容器不接收密钥。不要提交该文件或输出 `compose config` 展开的凭据。用 `compose config --quiet` 检查配置。

同源入口默认 `https://localhost:8443`，仅绑定回环，便于验收。真实内网部署时设置 `FIN_WEB_BIND` 为指定内网地址，`FIN_WEB_ORIGIN` 为准确 HTTPS 来源，`FIN_WEB_PORT` 为入口端口；不开放 API/Worker 端口。不应直接作为公网服务。

将机构信任的证书及私钥以 `server.crt` 和 `server.key` 放到 `.local/web-tls/`（或设置 FIN_WEB_TLS_DIR 为专用绝对目录），只读挂载。证书必须覆盖真实域名。验收可用 `python deploy/web/create_local_tls.py` 创建 14 天 localhost 自签证书，测试客户端显式信任该证书；脚本不安装根证书。自签证书不等同于已完成机构浏览器信任配置。

```powershell
& $docker --context desktop-linux compose -f compose.web.yaml config --quiet
& $docker --context desktop-linux compose -f compose.web.yaml build
& $docker --context desktop-linux compose -f compose.web.yaml up -d
& $docker --context desktop-linux compose -f compose.web.yaml ps
& $docker --context desktop-linux compose -f compose.web.yaml exec api python -m fin_agent.web create-user administrator --admin
& $docker --context desktop-linux compose -f compose.web.yaml exec api python -m fin_agent.web create-user researcher
```

账号命令交互输入密码，不在参数传递。迁移单次服务成功才启动 API/Worker，API 健康后启动入口；API 检查 HTTP 与数据库，Worker 检查最近 15 秒心跳。API 和 Worker 以 UID 10001、只读根文件系统运行。所有持久化信息位于独立本机卷，不能把 SQLite 放到多机共享/NFS 上。

Nginx 提供 SPA、同源 `/api` 和 TLS；SSE 禁止代理缓冲、缓存和 gzip，读取超时一小时。首版单 API、单 Worker 调度器、两个执行子进程、排队 16。CLI 的原生 Harness 执行链与 Web 的 LangGraph 执行链分别保留。

## 一致备份与保留

```powershell
& $docker --context desktop-linux compose -f compose.web.yaml exec api python -m fin_agent.web backup /data/backups/manual-20261107.sqlite3
& $docker --context desktop-linux compose -f compose.web.yaml exec api python -m fin_agent.web cleanup --backup-dir /data/backups
```

目标必须是新文件。备份生成 `.sqlite3` 与 `.sqlite3.json` 校验清单，可在线执行；清理只删除本数据库来源且校验完整、超过 7 天的备份，不删除其他文件。此命令不会自动创建定时备份，运维需纳入现有备份调度。灾备需复制备份、清单及**最新** `/data/web.sqlite3.deletions.jsonl` 到独立安全存储；只留同卷备份不能抵御卷损坏。外部删除日志不得被旧备份覆盖。

## 升级与回滚

先在线备份，再停止 Web/API/Worker，保存当前镜像 ID/标签；为新构建设置唯一 FIN_WEB_TAG。确保独立迁移任务完成再启动。升级期间不要并行运行新旧 API 或 Worker。

```powershell
& $docker --context desktop-linux compose -f compose.web.yaml stop web api worker
& $docker --context desktop-linux compose -f compose.web.yaml run --rm --no-deps migrate
& $docker --context desktop-linux compose -f compose.web.yaml up -d
```

恢复先停止同一项目所有 API/Worker，再执行：

```powershell
& $docker --context desktop-linux compose -f compose.web.yaml stop web api worker
& $docker --context desktop-linux compose -f compose.web.yaml run --rm --no-deps api restore /data/backups/manual-20261107.sqlite3
& $docker --context desktop-linux compose -f compose.web.yaml up -d
```

恢复校验来源路径身份及 SHA256，在临时库迁移和重放删除后原子替换；现存库会先生成 pre-restore 回滚备份。缺失主数据库也可恢复。旧未结束任务转为中断，清除进程归属和旧登录，用户重新登录；不会自动调用模型。外部删除日志校验失败时拒绝恢复。损坏的现存库应先由运维保留原始文件，不能据此直接宣称支持任意损坏原位修复。

应用回滚需恢复与旧镜像匹配的升级前备份，再使用保存的旧镜像标签；不执行破坏性 downgrade。备份身份绑定容器内数据库绝对路径 `/data/web.sqlite3`，恢复到不同路径应先另行制定迁移方案，不能修改校验清单绕过校验。

## 隔离验收

`deploy/web/compose.qa.yaml` 只用于 `-p fin-agent-web-qa` 的独立测试项目：同样的镜像、代理和 Worker，通过回环 `http://127.0.0.1:8180` 做浏览器交互测试，显式开启仅回环有效的开发 Cookie。不得将该覆盖配置用于真实内网入口。验收结束停止测试项目，保留卷和报告；HTTPS 主配置不带这个覆盖文件。

```powershell
& $docker --context desktop-linux compose -p fin-agent-web-qa -f compose.web.yaml -f deploy/web/compose.qa.yaml up -d
& $docker --context desktop-linux compose -p fin-agent-web-qa -f compose.web.yaml -f deploy/web/compose.qa.yaml stop
```

排障先查看 `compose ps` 和各服务日志，再按管理员运行编号检查独立模型/工具调用。Worker 重启会中断旧工作，API 重启不影响执行。不得以重启 Worker 作为浏览器断线的常规处理方式。

代理在 Docker DNS 上每 5 秒重新解析 API，支持 API 独立重建后的新地址。SSE 禁用 postpone_output，TLS 发送缓冲为 4k；相关语义可核对 [Nginx 代理缓冲](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering)、[发送延后](https://nginx.org/en/docs/http/ngx_http_core_module.html#postpone_output)、[TLS 缓冲](https://nginx.org/en/docs/http/ngx_http_ssl_module.html#ssl_buffer_size)。迁移依赖启动顺序参考 [Docker Compose 官方文档](https://docs.docker.com/compose/how-tos/startup-order/)。
