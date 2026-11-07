# 项目记忆

## Docker Desktop（用户指定）

- 使用 Windows 本机安装的 Docker Desktop。所有 Docker 命令从 Windows PowerShell 调用 Docker Desktop 提供的 `docker.exe`。
- Linux 容器使用 Docker Desktop 的 `desktop-linux` context；执行构建、启动、检查和部署命令时显式指定 `--context desktop-linux`，并先确认其属于 Docker Desktop。
- 禁止通过 `wsl` / `wsl.exe` 执行 Docker 命令；禁止安装、启动或使用 WSL 发行版内独立的 Docker CLI、Docker Engine 或 dockerd。
- Docker Desktop 不可用时，诊断并恢复本机 Docker Desktop；不得改用 WSL Docker 作为替代环境。
- 保留现有业务卷、配置、镜像和回滚能力。故障恢复不得擅自删除业务卷、重置 Docker 数据、迁移磁盘镜像或切换 Desktop 后端。
- 此记忆约束操作入口和 Docker 服务归属，不授权修改 Docker Desktop 自身内部后端。需要更改后端时，先说明具体影响。

验证标准：实际命令由 Windows Docker Desktop CLI 执行，目标为已核实的 Docker Desktop context，过程中没有通过 WSL 执行 Docker。
