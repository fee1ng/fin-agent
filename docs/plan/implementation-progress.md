# 实施进度与复现

更新日期：2026-11-03。所有任务最终验收仍以各自清单为准。

最新完整回归：24 项通过（12.223s），git diff --check 通过。恢复复审指出并修复三项：SDK 异常中断需保留待核对标记；原生 aborted 必须恢复为 cancelled；恢复答案写入接收日志/索引前需复用脱敏器。对应测试已验证原生终态能够纠正宿主等待异常，以及测试 SMTP 秘密不被恢复过程重新写入 receipts/SQLite。这不证明原生模型流的持久化前脱敏已完成。

本轮增量：新增跨进程运行租约与离线 `trace recover`，新提问前自动核对该会话遗留运行。跳过活跃请求；遗留运行仅从确认落盘的原生事件恢复，工具结果不明时标记 outcome_unknown，不重放。接收日志半写尾部先完整归档，保留已提交前缀后继续恢复。统一了宿主与查询索引的状态折叠函数。完整回归现为 23 项通过；真实 Harness 原生终态恢复、跨进程强杀和 queued 取消均有测试。此进度不代表运行中取消或执行预算已经完成。

## 当前实现

- 已加入可显式选择的 HarnessAgent，由官方 SDK/runtime 执行 Agent 循环。
- 接收记录先 fsync，再执行；逐提问有 run_id/input_id，重复 input_id 返回原运行，跨进程会话锁串行执行。
- 原生输入消息 ID 关联原生 turn；工具调用与原生事件序号保留，最终确认落盘后才报告完成。
- save/load 保存原生会话引用；clear 创建新上下文并保留历史。
- 提供离线 trace list/show/export/reindex；SQLite 索引可删除重建，查询展示持久化确认与缺口警告。
- 工具桥处理配置请求、结构化异常与部分业务错误字符串；输入、工具结果和工具日志有基础脱敏。

## 已执行验证

Docker Desktop `desktop-linux` 的独立开发容器中：

```text
python -m unittest discover -s tests -v
19 tests: OK
python -m fin_agent.main trace --trace-dir /tmp/fin-trace-cli-check list
返回空列表和 receipt_watermark=-1，无模型配置要求
```

真实 Harness 测试覆盖提问、工具调用、去重、保存/恢复、两个原生 turn 的区分、模型服务停止后的查询导出与索引重建。SDK 关闭时产生未关闭文件句柄的 ResourceWarning，后续需核对上游资源释放；当前不隐藏警告。

独立审查后的修复：固定每次提问的会话 ID，避免 yield 后切换会话造成错归；工具筛选遍历全部事件页；统一执行与查询的会话 ID 校验；日志 flush 保留不完整行以防秘密跨写入泄漏；查询检查原生序号缺口与重复，损坏 JSONL 明确失败。已补会话切换、分页筛选、分段日志与删除中间工具结果的回归。最后一项额外故障注入测试通过，`git diff --check` 通过。

## 尚未完成

- 全模型流、SDK stderr 和上游异常的持久化前脱敏及秘密扫描。
- 金融工具全量接入、写操作身份和后台任务来源。
- 队列公平性、执行预算和取消、崩溃恢复及 outcome_unknown 处理。
- 大输出附件、容量管理、性能测量、依赖来源校验值锁定。
- 旧会话 imported_context 迁移、完整运行版本快照。
- 独立审查修复正在进行；生产镜像构建、真实 DeepSeek smoke、切换及回滚尚未执行。

当前默认仍为旧引擎；开发中的 Harness 通过 FIN_AGENT_ENGINE=harness 显式选择。生产 Docker 容器与业务卷未切换。
