# Executor + Local Worker 可靠执行

生产环境以 PostgreSQL 为唯一状态源。`jobs.id` 是群聊级 `task_id`，`tasks.id`
是一次执行的 `run_id`；兼容字段 `job_id` 暂时等同于 `run_id`。

## 数据库升级

```powershell
$env:DATABASE_URL = "postgresql+psycopg://agent:agent@127.0.0.1:5432/agent_platform"
python -m alembic upgrade head
```

迁移 `002_reliable_worker` 添加：

- `(tenant_key, chat_id)` 的群任务唯一索引；
- 飞书消息、卡片命令的幂等表；
- Run 的阶段、Attempt、验证及交付字段；
- 带租约、心跳和 fencing token 的 `worker_jobs`；
- 按 `(run_id, attempt_no, sequence)` 去重的结构化事件。

历史 JSON 文件不再是生产运行时回退。首次上线可先执行：

```powershell
python scripts/migrate_legacy_state.py `
  --database-url $env:DATABASE_URL `
  --tasks data/tasks.json `
  --queue data/local_worker_queue.json
```

脚本是幂等的，并在原文件旁生成带时间戳的只读迁移备份。旧 `claimed`
记录会进入 `needs_attention`，不会自动覆盖遗留工作区。

## Worker 协议

Worker 使用以下接口，均要求 `Authorization: Bearer <LOCAL_WORKER_TOKEN>`：

- `POST /v1/worker/jobs/claim`
- `POST /v1/worker/jobs/heartbeat`
- `POST /v1/worker/jobs/events`
- `POST /v1/worker/jobs/complete`

除 claim 外，请求必须携带 `run_id`、`attempt_no` 和 `lease_token`。默认租约
45 秒、心跳 10 秒；旧 Worker 的事件或完成回调会收到 HTTP 409。

Local Worker 将每个群任务固定到：

```text
<LOCAL_WORKER_WORKTREE_ROOT>/<repo-slug>/<task_id>
```

Claude 使用 `--output-format stream-json --verbose`。结构化摘要写入 PostgreSQL，
原始 JSONL 写入 `LOCAL_WORKER_LOG_ROOT`，不会进入目标仓库。Claude 不负责
commit、push 或创建 MR，这些副作用由 Worker 在验证通过后幂等执行。

## 仓库验证策略

在 `repo_catalog` 中声明固定验证命令，命令来自管理员策略而不是模型输出：

```json
{
  "repo_catalog": {
    "owner/repo": {
      "executor": "local_worker",
      "local_path": "F:/repos/project",
      "verify_commands": [
        "python -m pytest -q",
        "python -m compileall -q src"
      ]
    }
  }
}
```

验证失败会把错误发送回同一个 Claude Session，默认最多修复两轮。仍失败、
租约丢失或恢复现场不明确时，Run 进入 `needs_attention`，分支和 worktree 保留。
