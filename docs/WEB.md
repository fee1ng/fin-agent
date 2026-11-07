# Web 应用开发与运行

已实现登录、隔离会话、后台 Agent、持久化 SSE、故障恢复、管理员调用时间线、数据保留及一致备份。容器部署说明见 [部署手册](WEB_DEPLOYMENT.md)，验收状态以 [进度记录](plan/web-chat/progress.md) 为准。

本轮按用户确认以本机 HTTPS 验收交付，入口 `https://localhost:8443`，仅绑定 `127.0.0.1`；七项计划已完成。内网部署配置保留为备用，不要求对内网或外网开放。

## Python 环境

Python 3.12 已验证。推荐为项目单独创建虚拟环境并安装：

```powershell
python -m venv .local/web-venv
.local/web-venv/Scripts/python.exe -m pip install -r requirements-web.txt
```

也可安装包含 Web 可选依赖的包 `pip install ".[web]"`，之后使用 `fin-agent-web` 命令。

## 数据库与管理员命令

Web 数据库独立于 CLI 的用户配置，默认 `.local/web.sqlite3`。迁移必须在启动 API 前单独执行，多个服务不可同时运行迁移。

```powershell
$env:FIN_WEB_DATABASE = 'E:\project\fin-agent\.local\web.sqlite3'
python -m fin_agent.web migrate
python -m fin_agent.web create-user researcher
python -m fin_agent.web create-user administrator --admin
python -m fin_agent.web disable-user researcher
python -m fin_agent.web enable-user researcher
```

创建账号时交互输入密码和确认，密码不少于 12 字符，不通过命令参数传递。禁用或重新启用账号都会撤销其已有登录。账号名使用 3–64 位字母、数字、点、下划线或连字符，不区分大小写。

## 本地开发

仅回环地址允许显式开启 HTTP 开发 Cookie。下面的来源必须与浏览器访问的地址完全相同，`localhost` 和 `127.0.0.1` 不可混用。

```powershell
$env:FIN_WEB_DEV_HTTP = '1'
$env:FIN_WEB_ORIGIN = 'http://127.0.0.1:5173'
python -m fin_agent.web serve
```

另一个终端进入前端目录，Node 24 和 pnpm 11 已验证：

```powershell
pnpm install --frozen-lockfile
pnpm dev
```

浏览器打开 `http://127.0.0.1:5173`。Vite 将 `/api` 转发给 `127.0.0.1:8000`。前端凭据只使用 HttpOnly Cookie，CSRF token 仅在页面内存保存，不使用 localStorage 存密码或认证令牌。

再启动独立执行器（与 API 使用相同 FIN_WEB_DATABASE）：

```powershell
python -m fin_agent.web worker
```

执行器使用现有服务端模型配置，不进入交互配置向导；未配置凭据时任务会失败。通过环境变量或专用服务配置提供模型及数据源密钥，不把密钥交给浏览器。每次 Worker 启动只允许一个调度进程，内部默认为两个隔离子进程执行不同会话。同会话已有活动问题时，第二次提交会被拒绝。

可设置 FIN_WEB_CONCURRENCY（默认 2）、FIN_WEB_QUEUE_LIMIT（默认 16）与 FIN_WEB_RUN_TIMEOUT（默认 300 秒）。问题请求最长 8000 字符，进程间单条消息限制 2 MB，工具追踪结果仅保留脱敏摘要。关闭页面不会停止 Worker。

API 可以独立重启。Worker 重启会先核对并终止旧 PID/创建时间匹配的执行进程，再将旧运行和排队任务标记中断；不会自动重放。用户可在恢复服务后点击“重试分析”，创建新运行并沿用原问题。只有首次安装且从未启动过 Worker 的新队列会由首次启动接收。出现执行器失联提示时，先恢复 Worker，再查看最终状态，不要依据浏览器断线判断任务已经停止。

重试接口为 POST `/api/v1/runs/{id}/retry`，需要新的 Idempotency-Key；同一次重试遇到网络错误时应沿用该键。只允许重试最近失败或中断的运行；同会话之后已有新问题时，旧运行不可重试，以避免混入错误上下文。界面会保留中断前的部分正文，模型上下文仅使用已成功提交的轮次。

升级旧 Web 数据库时先停止旧 Worker 和 API，再执行 migrate；当前迁移版本为 0005_retention，不修改原 CLI 数据。HTTP POST 创建任务需要 Idempotency-Key；SSE `/api/v1/runs/{id}/events` 只订阅已持久化事件，不执行模型调用。瞬时 executor.status 事件用于显示执行服务心跳，不占用持久化运行事件序号。

开发 Cookie 的 Path 为 `/api`，所以只有 API 请求携带登录令牌。所有变更请求（含登录）必须带匹配的 Origin；已登录变更请求还需 X-CSRF-Token，值来自登录或 `/api/v1/auth/me` 的返回。

## 内网部署配置

生产环境关闭 `FIN_WEB_DEV_HTTP`，将 `FIN_WEB_ORIGIN` 设置为真实 HTTPS 站点来源（无尾斜线或路径），反向代理同源提供前端资源和 `/api`。Cookie 默认 Secure、HttpOnly、SameSite=strict。不为方便测试而在内网 IP 上开启明文开发 Cookie。

Docker 执行前必须读取项目 `agnets.md` 并使用 Windows Docker Desktop CLI；参见部署手册中的独立 Compose 项目和卷配置。

## 管理诊断与保留

管理员通过聊天侧栏进入“运行记录与诊断”，可按用户、状态、北京时间、工具及会话/运行编号筛选。逐调用时间线保留关联模型编号、tool_call_id、实际起止和耗时、首片段类型、脱敏参数/摘要及错误。SDK 内部隐藏重试包含在模型请求耗时内；未返回的 token 用量显示“未知”，0 单独保留。普通用户不能调用管理员接口。

Worker 每小时清理过期终态数据，每轮最多 500 次运行：详细 span 默认终态后 30 天、SSE 恢复事件 24 小时。聊天与已提交模型上下文保留至用户删除。`FIN_WEB_TRACE_DAYS`、`FIN_WEB_EVENT_HOURS` 和 `FIN_WEB_BACKUP_DAYS` 分别覆盖默认值（30/24/7）。追踪过期后保留调用数量等聚合，页面明确提示；恢复事件过期后从快照恢复。

备份命令使用 SQLite backup API，不复制活跃 WAL 文件。恢复必须停止 API/Worker；共享卷上的进程存活文件锁也能跨容器拒绝在线覆盖。当前仅支持一个 API 进程和一个 Worker 调度器。删除日志 `<database>.deletions.jsonl` 位于数据库之外，成功确认删除前 fsync；恢复旧备份会重放当前日志并撤销备份里的旧登录。务必一起保存最新删除日志，丢失它将无法保证重放备份之后发生的删除。

## 验证

```powershell
python -m unittest discover -s tests -v
pnpm test
pnpm build
```

前端命令在前端目录执行。数据库授权及故障行为使用临时 SQLite 和 FastAPI TestClient 验证；前端交互使用受控 API 响应，不能替代真实部署或真实模型调用验证。

依赖构建脚本仅批准 esbuild；pnpm 锁文件固定前端实际依赖版本。Linux/Python 3.12 容器使用包含全部传递依赖和哈希的 requirements-web.lock；Windows 开发环境使用 requirements-web.txt。Docker 基础镜像按摘要固定，依赖升级后需重新验证。
