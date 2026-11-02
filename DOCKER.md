# Docker Desktop 运行说明

本项目是终端交互应用，没有 Web 服务或需要映射的端口。
以下命令在项目目录执行，使用 Docker Desktop 的 Linux 引擎。
若 `docker` 不在 PATH，PowerShell 可先执行：

```powershell
$env:PATH += ";$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin"
```

## 构建及启动

```powershell
docker --context desktop-linux compose build
docker --context desktop-linux compose run --rm fin-agent
```

如果 Docker Hub 认证连接超时，可在当前 PowerShell 会话为 Docker CLI 指定
本机已经运行的 HTTP 代理，然后重新构建。本机验证使用的代理端口为 10808：

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:10808"
$env:HTTP_PROXY = "http://127.0.0.1:10808"
docker --context desktop-linux compose build
```

首次启动按照终端向导配置真实模型 API Key。默认 AkShare 数据源不需要
Tushare Token；选择 Tushare 数据源时才需要。配置、持仓和预警任务保存在
`fin-agent-data` 命名卷的 `/data/fin-agent` 下，退出容器后仍保留。
输入 `exit` 退出对话。

如果容器已经通过 `compose up -d fin-agent` 启动，可直接连接现有终端：

```powershell
docker --context desktop-linux compose attach fin-agent
```

当前交付的容器正在等待首次配置。连接后若没有出现提示，按一次 Enter
刷新交互，然后按向导输入配置。可按 Ctrl+P、Ctrl+Q 脱离终端并保持容器运行。

已有环境配置文件也可在运行时传入（不要把真实密钥写入镜像）：

```powershell
docker --context desktop-linux compose run --rm --env-from-file .env fin-agent
```

持久化配置中的值会覆盖环境变量；修改已有配置请使用应用的配置功能。
连接本机 Ollama 等服务时使用 `http://host.docker.internal:11434/v1`。

## 后台预警 Worker

先完成上述交互配置，再启动共享同一配置卷的 Worker：

```powershell
docker --context desktop-linux compose --profile worker up -d worker
docker --context desktop-linux compose logs --tail 50 worker
docker --context desktop-linux compose stop worker
```

Worker 没有聊天界面，只有存在预警任务且配置有效时才有实际监控工作。

## 快速验证

```powershell
docker --context desktop-linux run --rm fin-agent:local --version
docker --context desktop-linux run --rm fin-agent:local --help
```

镜像以非 root 用户运行。不要使用 `compose down -v`，除非确实要删除配置和持仓卷。
