# 当前发布范围评估

依据用户最新要求，以“真实 Harness 驱动金融 Agent，并可靠追踪工具调用”为本次主要目标；不把全部扩展运维功能作为首版发布条件。

## 可以延期

严格 FIFO（本地单会话 CLI 暂只要求串行）、专用大结果附件（原生日志已保留完整结果）、容量管理 UI、固定 p95 性能基准、旧会话自动迁移、后台任务调度系统迁移。这些保持待办，不再单独阻塞首版。

## 必须保留

完整金融工具可发现和调用；真实原生日志与运行身份关联；最终持久化确认及异常状态不伪造成功；凭据不得写入原生轨迹；容器内依赖及插件完整、非 root、持久化业务卷不丢失；隔离 smoke 与真实路由验证；备份和回滚。

## 本轮证据

- 已将 MCP 默认工具清单从时间工具扩展为完整项目清单，禁用 shell/file tools，增加金融助手提示词。真实 Harness 流程测试通过，后续发布需再核对全部业务场景。
- Dockerfile 已加入固定 SDK/runtime/MCP 依赖，打包 `.mjs` 插件，并支持通过 BASE_IMAGE 选择本地基础镜像。
- `python -m tests.harness_release_gate` 用假密钥验证：application_answer_redacted=true，但 native_secret_leaks=1，deployment_allowed=false。确认持久化前脱敏是实际发布缺口，不能延期。
- 标准候选构建遭 Docker Hub 连接超时；已保存本地开发依赖快照 `fin-agent:harness-deps-20261103`（sha256:c5b68a2dfa5c71829ee8b8163469de6d67b77a8b05598a6471b805571101c01e）。这只是依赖快照，不是通过验收的发布镜像。
- 本地快照候选构建随后因 Docker 后端 EOF 失败；检查发现 C 盘 Free=0，Docker Desktop 报 unable to start。候选镜像构建未完成，未执行生产切换。
- 为缓解满盘，18 个已轮转的 Docker 旧诊断日志（18,872,949 bytes）无损归档到项目 `.local/docker-recovery-logs`，已加入 Git 忽略。未删除或移动业务卷及 Docker VHDX。归档后 C 盘仅剩约 3 MiB，仍不足以正常构建。

结论：无需等待全部原计划，但本次实际核心门禁失败且 Docker 主机磁盘不足，不能宣布可部署。先修复原生脱敏及 Docker 主机可用空间，再沿最小发布范围完成镜像与部署验证。
