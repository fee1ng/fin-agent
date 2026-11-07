# 最新 Harness 镜像与容器

构建及核验日期：2026-11-03。通过 Windows Docker Desktop CLI，context 为 desktop-linux；未通过 WSL 执行 Docker。

- 镜像：`fin-agent:harness-latest`
- 镜像 ID：`sha256:a19edc1e4839e31dac906c3026d24ce34b78d76bf6f34490f73a189fba06dfe5`
- 容器：`fin-agent-harness-latest`
- 容器 ID：`f414907816c25ac8c691bc6202de8d97dccbc523067a9b32d095867933a8c042`
- 核验状态：running；实际引擎 harness；UID 10001。
- SDK/runtime：0.2.1rc2；MCP：1.30.0；持久化插件存在于安装后的 Python 包中。
- 数据卷：`fin-agent-harness-data:/data`。已有配置及业务数据从旧业务卷只读复制，原卷保留，凭据未输出。

## 构建证据

先前满盘时保存的 harness-deps 快照中的运行时不能执行，已弃用于本次产物。重新通过 pip 下载固定 SDK/runtime/MCP 及依赖 wheel 到 `.local/harness-wheels`，从干净本地镜像 `fin-agent:dev-v1.1.0` 构建，pip check 通过。

本机 BuildKit 对旧快照出现元数据读取异常，因此本次使用 Docker Desktop 的经典构建器。重新构建命令：

```powershell
$env:DOCKER_BUILDKIT = '0'
docker --context desktop-linux build --network=none -f Dockerfile.harness -t fin-agent:harness-latest .
```

`Dockerfile.harness` 需要已准备好的本地基础镜像与 wheel 目录。普通联网构建可使用主 Dockerfile。

成品测试从 `/tmp` 运行，只挂载测试文件，使用镜像中已安装的应用与插件；24 项全部通过，包含真实 Harness/MCP、去重、恢复及原生日志缺口检查。启动日志确认 `engine=harness`。已有 DeepSeek 凭据配置存在，仅核验布尔状态；本次未发送真实付费模型请求。

## 使用

```powershell
docker --context desktop-linux attach fin-agent-harness-latest
```

按 Ctrl+P、Ctrl+Q 退出 attach 并保持容器运行。原有容器和业务卷保留。

独立启动配置为 `compose.harness.yaml`；其数据卷为 external，避免 Compose 删除原始数据。

本次完成的是可启动的最新开发版本打包；不表示全部追踪规格验收完成。原生日志持久化前脱敏门禁仍未通过，执行预算等剩余项见实施进度记录。

## 用户授权清理旧版本

随后按用户要求删除旧开发容器 `9618922091fa`、旧运行容器 `f235f86ab885`，以及 `fin-agent:local`、`fin-agent:dev-v1.1.0`、损坏的 `fin-agent:harness-deps-20261103` 和失败构建产物。核验后仅剩最新 Harness 容器与镜像，全部数据卷保留；旧镜像回滚入口已按用户此次授权移除。

离线 Dockerfile 默认基础镜像已调整为保留的 `fin-agent:harness-latest`，用于后续增量构建。全新机器首次构建使用主 Dockerfile；本次首轮使用的旧基础标签已清理，不再假设它存在。
