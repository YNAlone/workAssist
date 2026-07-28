# Executor + Local Worker 可靠性改造实施说明

> 文档版本：1.0  
> 完成日期：2026-07-27  
> 适用项目：workAssist / Feishu Claude Automation  
> 实现语言：Python 3.11+  
> 状态存储：PostgreSQL  
> 代码执行器：Claude Code CLI

## 1. 改造目标

本次改造保留现有 Python Executor、Local Worker 和 CI Executor 架构，不引入
Temporal 或其他工作流引擎。目标是把原来依赖 JSON 队列、共享代码目录和单次
进程执行的方案，升级为能够安全恢复、重复调用不产生副作用的确定性执行流程。

核心业务约束如下：

- 一个飞书群聊对应一个长期业务 Task。
- 一个 Task 对应一个固定 Git 分支和一个持久化 worktree。
- 用户首次执行、补充需求、修复 CI 等操作分别产生新的 Run。
- Worker 对同一个 Run 的重新领取属于新的 Attempt，不创建新 Task 或 Run。
- Claude Code CLI 负责理解需求和修改代码。
- Worker 控制器负责验证、commit、push、创建 PR/MR 和状态持久化。
- 单次 Run 失败不会销毁 Task；系统自动恢复一次，无法安全恢复时保留现场并进入
  `needs_attention`。

## 2. 最终身份模型

系统将身份拆分为四层：

```text
Feishu Chat
└── Task / jobs.id
    ├── Run / tasks.id
    │   ├── Attempt 1
    │   └── Attempt 2
    └── Run / tasks.id
        └── Attempt 1
```

### 2.1 Chat

飞书群聊是业务任务入口，使用以下业务键唯一标识：

```text
(tenant_key, chat_id)
```

数据库通过部分唯一索引 `uq_jobs_tenant_chat` 保证同一个租户中的同一个群只能
创建一个 Task。应用层的“先查询再创建”不是最终保障；并发竞争仍由数据库唯一
索引裁决。

### 2.2 Task

`jobs.id` 是公开、稳定的 `task_id`，保存：

- 飞书租户、群聊和初始负责人；
- 仓库、基础分支和工作分支；
- 持久 worktree 路径；
- 当前 Run；
- Claude Session ID；
- 长期任务状态和版本号。

Task 的生命周期长于单次代码执行。单个 Run 失败时，Task 进入
`awaiting_retry` 或 `needs_attention`，不会因为一次失败永久结束。

### 2.3 Run

`tasks.id` 是 `run_id`。以下行为会产生新 Run：

- 首次确认执行；
- 用户补充需求后再次执行；
- 主动重试或重新验证；
- CI 失败后的修复。

Run 保存执行阶段、迭代号、Attempt、验证结果、commit SHA、远端 SHA、MR 地址
和 CI 状态。

### 2.4 Attempt

Attempt 是 Worker 对同一 Run 的一次实际领取：

- 首次领取时 `attempt_no = 1`；
- Worker 崩溃或可安全恢复时，继续使用原 `run_id` 并增加 `attempt_no`；
- 默认最多自动恢复一次；
- 每个 Attempt 都有独立 `lease_token`；
- 旧 Attempt 的回调不能覆盖新 Attempt。

## 3. 整体架构

```mermaid
flowchart LR
    U["飞书群用户"] --> F["Feishu Events / Actions"]
    F --> I["消息幂等 Inbox"]
    I --> T["Task<br/>jobs.id"]
    T --> R["Run<br/>tasks.id"]
    R --> D["ExecutorDispatcher"]
    D --> CI["GitHub Actions / GitLab CI"]
    D --> Q["PostgreSQL Worker Queue"]
    Q --> W["Local Worker"]
    W --> WT["持久 Git Worktree"]
    W --> C["Claude Code CLI<br/>stream-json"]
    C --> V["验证与修复循环"]
    V --> G["Commit / Push / PR-MR"]
    W --> E["Worker Events"]
    E --> T
    G --> F
```

生产环境启动时必须连接 PostgreSQL。平台初始化失败会阻止服务启动，不再静默
降级为 JSON 状态源。JSON 文件仅保留为一次性迁移输入和明确的测试兼容模式。

## 4. 数据库变更

迁移文件：

```text
alembic/versions/002_reliable_worker.py
```

### 4.1 `jobs`

在原表基础上增加：

| 字段 | 用途 |
| --- | --- |
| `tenant_key` | 飞书租户标识 |
| `repo` | Task 固定仓库 |
| `base_branch` | 基础分支 |
| `work_branch` | Task 固定工作分支 |
| `worktree_path` | Task 固定 worktree |
| `current_run_id` | 当前 Run |
| `claude_session_id` | 最近可恢复的 Claude Session |
| `version` | Task 状态版本 |

唯一索引：

```text
uq_jobs_tenant_chat(tenant_key, chat_id) WHERE chat_id <> ''
```

空 `chat_id` 的非飞书 API 任务不参与群聊唯一约束。

### 4.2 `tasks`

在原表基础上增加：

| 字段 | 用途 |
| --- | --- |
| `command_key` | 确认、重试或卡片操作幂等键 |
| `iteration` | Task 内 Run 序号 |
| `phase` | 当前执行阶段 |
| `attempt_no` | 当前 Attempt |
| `verification` | 验证命令与结果 |
| `commit_sha` | 本地提交 SHA |
| `remote_sha` | 远端分支 SHA |
| `mr_url` | PR/MR 地址 |
| `ci_status` | CI 状态 |

约束：

```text
UNIQUE(job_id, iteration)
UNIQUE(job_id, command_key) WHERE command_key <> ''
```

### 4.3 `task_messages`

该表是飞书事件 Inbox，保存原始事件、处理状态和最终响应。

幂等约束：

```text
UNIQUE(tenant_key, event_id) WHERE event_id <> ''
UNIQUE(tenant_key, message_id) WHERE message_id <> ''
```

处理状态：

```text
received → processing → processed
                    └→ failed → processing
```

重复事件的处理规则：

- 已 `processed`：直接返回第一次持久化的响应；
- 正在 `processing`：返回现有 `task_id` 和处理状态；
- `failed`：允许飞书重投再次领取；
- 首次投递：在数据库中绑定群 Task 后再进入业务处理。

### 4.4 `task_commands`

保存卡片确认、重试等用户命令与 Run 的映射：

```text
UNIQUE(task_id, command_key)
```

同一个按钮事件或确认命令只能创建一个 Run。

### 4.5 `worker_jobs`

该表替代生产环境 JSON Worker 队列，核心字段包括：

- `task_id`、`run_id`；
- Worker 执行负载；
- `status`、`phase`；
- `attempt_no`、`recovery_count`；
- `lease_token`、`lease_expires_at`、`heartbeat_at`；
- `worker_id`；
- 持久化执行结果和错误；
- `terminal` 终态标志。

每个 Run 只能有一个 Worker Job：

```text
UNIQUE(run_id)
```

### 4.6 `worker_events`

保存标准化 Worker 和 Claude 事件：

```text
UNIQUE(run_id, attempt_no, sequence)
```

Worker 因网络超时重复上报同一事件时，服务端返回已存在结果，不会重复写入。

## 5. Task、Chat 与命令幂等

### 5.1 同一群聊并发创建

流程如下：

1. 按 `(tenant_key, chat_id)` 查询 Task。
2. 未找到时尝试插入新 `jobs` 记录。
3. 如果两个请求并发插入，数据库唯一索引只允许一个成功。
4. 失败请求捕获唯一冲突并读取获胜记录。
5. 两个请求最终得到相同 `task_id`。

### 5.2 飞书重复投递

消息处理、Task 绑定和 Inbox 写入使用同一数据库事务。服务崩溃时：

- 事务未提交：飞书重试可以重新处理；
- Inbox 已提交、业务未完成：记录进入 `failed` 后允许重领；
- 业务已完成：重复投递返回第一次保存的结果。

### 5.3 卡片重复点击

服务使用 `event_id`、`message_id`、卡片 action 标识或 API
`idempotency_key` 生成命令键。同一 `command_key` 只能关联一个 Run。

### 5.4 Worker 重复派发

Worker 队列按 `run_id` 唯一。Executor 重复调用 enqueue 时返回原 Worker Job，
不会产生第二条队列记录。

### 5.5 完成回调重复或乱序

Worker 完成请求必须携带：

```json
{
  "run_id": "run-id",
  "attempt_no": 1,
  "lease_token": "opaque-fencing-token"
}
```

- 同一 Attempt 重复完成：返回已保存终态；
- 旧 Attempt 完成：返回 `409 stale_lease`；
- token 不匹配或已过期：拒绝更新；
- 新 Attempt 不会被旧 Worker 的迟到回调覆盖。

## 6. Worker 租约、心跳和恢复

### 6.1 领取

PostgreSQL 领取使用：

```sql
SELECT ...
FOR UPDATE SKIP LOCKED
```

多个 Worker 可以并发领取不同 Run，不会重复取得同一行。

领取成功后生成：

- 递增的 `attempt_no`；
- 不可预测的 `lease_token`；
- 默认 45 秒后的 `lease_expires_at`；
- 当前 `worker_id`。

### 6.2 心跳

默认每 10 秒续租一次。Worker 启动执行前会立即发送一次心跳，避免网络传输消耗
初始租约。

连续三次心跳失败时：

1. 标记本地 lease lost；
2. 终止正在运行的 Claude 子进程；
3. commit、push、MR 等后续副作用全部停止；
4. 迟到完成请求因 fencing token 失效而被拒绝。

### 6.3 自动恢复

默认只自动恢复一次：

- 有 Claude Session：使用 `--resume` 继续；
- 无 Session 且 worktree 干净：允许重新开始；
- 无 Session 且存在未提交代码：进入 `needs_attention`；
- 第二次租约过期：进入 `needs_attention`；
- 验证失败超过修复次数：进入 `needs_attention`。

安全重试只增加 Attempt，不更换 `run_id`、分支或 worktree。

### 6.4 用户取消

取消操作会：

- 将 Worker Job、Run 和 Task 更新为 `cancelled`；
- 清空当前 `lease_token`；
- 使 Worker 后续心跳立即失败；
- 由心跳线程终止 Claude 进程；
- 阻止 Worker 执行后续 Git 副作用。

## 7. 持久 Git Worktree

worktree 路径固定为：

```text
<LOCAL_WORKER_WORKTREE_ROOT>/<repo-slug>/<task_id>
```

行为规则：

- 源仓库只用于 fetch 和管理 worktree，不执行 `checkout -B`；
- 首次 Run 从远端工作分支或基础分支创建 worktree；
- 后续 Run 复用同一路径和分支；
- worktree 已存在但分支不一致时停止并进入人工处理；
- 不执行 reset、clean 或覆盖未提交代码；
- 不修改用户当前共享仓库工作区。

数据库中的 Task 持有最终工作分支。即使旧会话过期或创建新的兼容任务记录，
Worker enqueue 时也会恢复 Task 已有分支，避免为同一群创建第二个工作分支。

## 8. Claude Code CLI 执行

Worker 使用 `subprocess.Popen` 运行：

```text
claude -p
  --output-format stream-json
  --verbose
  --max-turns 50
  --model <model>
  --allowedTools Edit,Read,Write,Bash
  [--resume <session-id>]
  <prompt>
```

本次改造移除了：

```text
--dangerously-skip-permissions
```

### 8.1 结构化事件

Worker 逐行读取 `stream-json`：

- 提取事件类型和 Session ID；
- 将标准化事件写入 `worker_events`；
- 限制数据库单事件大小；
- 使用序列号实现事件重放幂等；
- 将 Session ID 同步到 Task，供恢复使用。

### 8.2 原始日志

原始 JSONL 保存在：

```text
<LOCAL_WORKER_LOG_ROOT>/<task_id>/<run_id>/attempt-<n>.jsonl
```

日志不位于目标 worktree，因此不会被 `git add -A` 提交。默认保留 14 天，只会在
配置的日志根目录内清理过期 `.jsonl` 文件。

## 9. 验证与自动修复

每个仓库通过策略声明管理员控制的验证命令：

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

验证流程：

```text
Claude 修改
→ 执行 verify_commands
→ 成功：进入 commit
→ 失败：把结构化错误发回同一 Claude Session
→ 再次验证
→ 最多修复两轮
→ 仍失败：needs_attention
```

验证命令来自仓库策略，不允许 Claude 动态决定交付门禁。

## 10. Git 和 PR/MR 幂等

### 10.1 Commit

- commit 前检查 worktree 是否有实际变更；
- commit message 包含 `run_id`；
- commit 后记录 SHA；
- 如果进程在 commit 后、事件持久化前崩溃，恢复时通过 HEAD commit message
  识别该 Run 已经提交，不重复 commit。

### 10.2 Push

- push 前使用 `git ls-remote` 查询远端分支 SHA；
- 远端 SHA 与本地 commit SHA 一致时跳过 push；
- push 后再次读取远端 SHA；
- Git URL 中的凭据在错误信息中自动脱敏。

### 10.3 PR/MR

GitHub 和 GitLab 创建变更请求前都会按以下条件查询：

- 仓库；
- source branch；
- target branch；
- opened 状态。

已有 PR/MR 时直接返回原地址。因此 Worker 在 MR 创建成功但数据库回写前崩溃，
恢复后不会创建第二个 MR。

## 11. Run 状态机

正常流程：

```text
queued
→ leased
→ preparing_worktree
→ running_claude
→ verifying
→ repairing
→ committing
→ pushing
→ opening_mr
→ succeeded
```

异常流程：

```text
running → awaiting_retry → queued
running → needs_attention
running → cancelled
running → failed
```

`failed` 是 Run 级终态。Task 会根据 Run 结果进入 `awaiting_retry` 或
`needs_attention`，而不是因一次 Run 失败丢失整个群任务。

## 12. Worker HTTP API

所有 Worker API 使用：

```http
Authorization: Bearer <LOCAL_WORKER_TOKEN>
Content-Type: application/json
```

### 12.1 入队

```http
POST /v1/worker/jobs
```

兼容接受 `job_id`，新协议使用 `task_id` 和 `run_id`。

### 12.2 领取

```http
POST /v1/worker/jobs/claim

{
  "worker_id": "worker-host-a"
}
```

无任务时返回：

```json
{
  "job": null
}
```

### 12.3 心跳

```http
POST /v1/worker/jobs/heartbeat

{
  "run_id": "run-id",
  "attempt_no": 1,
  "lease_token": "token",
  "phase": "running_claude"
}
```

### 12.4 事件

```http
POST /v1/worker/jobs/events

{
  "run_id": "run-id",
  "attempt_no": 1,
  "lease_token": "token",
  "sequence": 12,
  "event_type": "phase",
  "phase": "verifying",
  "payload": {}
}
```

### 12.5 完成

```http
POST /v1/worker/jobs/complete

{
  "run_id": "run-id",
  "attempt_no": 1,
  "lease_token": "token",
  "status": "completed",
  "result": {
    "commit_sha": "...",
    "remote_sha": "...",
    "pr_url": "...",
    "claude_session_id": "..."
  }
}
```

### 12.6 取消

```http
POST /v1/worker/jobs/cancel

{
  "run_id": "run-id",
  "reason": "cancelled by user"
}
```

## 13. 飞书交互与权限

### 13.1 群级会话

兼容会话查询由原来的：

```text
(chat_id, requester_id)
```

改为：

```text
chat_id
```

群内其他成员可以继续补充上下文，不会因为发送人不同创建第二个会话。

### 13.2 控制权限

以下操作只允许初始负责人或 `approval_requesters` 中的审批人：

- approve；
- confirm；
- rerun；
- cancel；
- cancel session。

示例：

```json
{
  "approval_requesters": [
    "ou_xxxxxxxxx"
  ]
}
```

### 13.3 用户可见状态

飞书任务卡片增加执行阶段，并支持：

- 执行中阶段展示；
- 错误详情；
- 等待重试；
- 需要人工处理；
- 原需求重试；
- 审批和取消。

## 14. 配置项

新增环境变量：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL 本机地址 | 唯一生产状态源 |
| `LOCAL_WORKER_ID` | 主机名 | Worker 身份 |
| `LOCAL_WORKER_LEASE_SECONDS` | `45` | 租约时间 |
| `LOCAL_WORKER_HEARTBEAT_SECONDS` | `10` | 心跳间隔 |
| `LOCAL_WORKER_MAX_RECOVERIES` | `1` | 自动恢复次数 |
| `LOCAL_WORKER_WORKTREE_ROOT` | `data/worktrees` | 持久 worktree 根目录 |
| `LOCAL_WORKER_LOG_ROOT` | `data/worker-logs` | Claude 原始日志目录 |
| `LOCAL_WORKER_LOG_RETENTION_DAYS` | `14` | 原始日志保留时间 |
| `LOCAL_WORKER_MAX_REPAIR_LOOPS` | `2` | 验证修复轮数 |

`LOCAL_WORKER_QUEUE_PATH` 只用于历史迁移和显式测试配置，不再是生产队列。

## 15. 数据迁移

### 15.1 执行 Alembic

```powershell
$env:DATABASE_URL = "postgresql+psycopg://agent:agent@127.0.0.1:5432/agent_platform"
python -m alembic upgrade head
```

建议迁移前备份 PostgreSQL。

### 15.2 迁移历史 JSON

```powershell
python scripts/migrate_legacy_state.py `
  --database-url $env:DATABASE_URL `
  --tasks data/tasks.json `
  --queue data/local_worker_queue.json
```

迁移脚本：

- 不删除原文件；
- 在原文件旁创建带 UTC 时间戳的备份；
- 重复执行不会重复创建已有 Run 或 Worker Job；
- 将无法证明 Worker 所有权的旧 `claimed` 记录标记为 `needs_attention`；
- 不自动清理或覆盖任何 Git 工作区。

### 15.3 推荐上线顺序

1. 停止旧 Orchestrator 和 Worker。
2. 备份 PostgreSQL 和 `data/*.json`。
3. 更新代码。
4. 执行 `alembic upgrade head`。
5. 执行一次 JSON 迁移脚本。
6. 配置 worktree、日志和验证命令。
7. 启动 Orchestrator。
8. 确认 `/health` 中 `db_ok=true`。
9. 启动一个 Worker，验证 claim 和 heartbeat。
10. 创建测试群任务并检查 Task、Run、worktree 和 MR。
11. 再逐步增加 Worker 数量。

## 16. 测试覆盖

新增可靠性测试覆盖：

- 同租户同群 Task 唯一；
- 飞书 event/message 去重；
- 卡片命令与 Run 幂等；
- Worker 重复 enqueue；
- Worker claim、heartbeat 和 fencing；
- Worker Event 重复序列；
- 重复 complete；
- 旧 lease token 拒绝；
- 安全重试复用相同 Run；
- 用户取消使 lease 失效；
- Claude stream-json 解析与原始日志；
- Claude Session resume；
- 移除危险权限参数；
- 持久 worktree 创建和复用；
- commit 后崩溃恢复识别；
- Windows JSON 队列兼容锁。

验证命令与结果：

```text
python -m compileall -q src tests scripts
结果：通过

python -m pytest -q
结果：60 passed

python -m alembic upgrade head --sql
结果：成功生成 PostgreSQL 001 → 002 完整迁移 SQL

git diff --check
结果：通过，仅有 Windows CRLF 转换提示
```

平台测试默认使用隔离 SQLite 数据库。CI 如果需要运行真实 PostgreSQL 集成测试，
可以设置：

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://agent:agent@127.0.0.1:5432/agent_platform_test"
python -m pytest tests/test_platform_m1.py -q
```

## 17. 兼容策略

- `job_id` 暂时作为 `run_id` 的兼容别名；
- 新响应同时提供 `task_id`、`run_id` 和兼容 `job_id`；
- Legacy Orchestrator 继续承担现有飞书需求澄清和卡片展示；
- PostgreSQL Task、Run 和 Worker Job 是可靠执行状态；
- Legacy JSON Task/Session 可作为兼容视图存在，但平台启动失败时不会降级为 JSON
  生产状态源；
- CI Executor 保留，回调统一映射到 Run 状态。

建议在下一个不兼容版本中删除：

- 外部调用对 `job_id` 的依赖；
- 生产配置中的 `LOCAL_WORKER_QUEUE_PATH`；
- 旧 JSON Task/Session 写入；
- Legacy callback 的字段别名。

## 18. 运维观察与排障

### 18.1 Worker 无法领取

检查：

- Orchestrator 是否连接正确的 `DATABASE_URL`；
- Worker token 是否一致；
- `worker_jobs.status` 是否为 `queued`；
- 是否已经达到自动恢复次数并进入 `needs_attention`。

### 18.2 Worker 不断收到 409

说明当前 Attempt 已失去租约。应停止该 Worker 的当前执行，不要手工重放旧
complete。检查：

- Worker 时间和网络；
- 心跳间隔是否小于租约；
- 是否有另一个 Worker 已恢复同一 Run；
- 用户是否取消了任务。

### 18.3 worktree 分支不匹配

系统不会自动 reset。处理步骤：

1. 停止相关 Run；
2. 检查数据库 Task 的 `work_branch`；
3. 检查实际 worktree 当前分支和未提交差异；
4. 人工确认保留或迁移差异；
5. 修正后重新触发 Run。

### 18.4 Claude 执行失败

优先检查：

- `worker_events` 中最后一个结构化事件；
- `LOCAL_WORKER_LOG_ROOT` 下对应 Attempt 的 JSONL；
- Task 的 `claude_session_id`；
- Worker 心跳和 lease 到期时间；
- 验证命令的 stdout/stderr 摘要。

### 18.5 push 或 MR 阶段崩溃

不要手工重复创建 MR。重新领取后系统会：

1. 检查本地 commit；
2. 比较远端 SHA；
3. 查询已有 PR/MR；
4. 仅补做缺失步骤。

## 19. 已知边界

- 本次实现没有自动合并 PR/MR。
- CI Executor 的外部平台任务取消仍依赖对应 CI 平台能力；Local Worker 已支持
  lease fencing 取消。
- PostgreSQL 迁移 SQL 已验证生成，但当前开发环境没有对真实生产数据库执行迁移。
- 原始 Claude JSONL 由本机保留策略清理；跨主机集中日志需要后续接入日志平台。
- `allowedTools` 仍包含 Bash，因为代码验证和仓库操作可能需要 shell；真正的交付
  副作用仍由 Worker 控制器执行。
- 飞书兼容 Orchestrator 仍保留 JSON Task/Session 视图，后续可将澄清会话内容也
  完整迁入 PostgreSQL，最终删除兼容存储。

## 20. 相关文件

| 文件 | 作用 |
| --- | --- |
| `src/agent_platform/db.py` | SQLAlchemy 表模型 |
| `src/agent_platform/store.py` | Task、消息和命令幂等 |
| `src/agent_platform/worker_store.py` | Worker 租约队列和 fencing |
| `src/agent_platform/orchestra.py` | Chat Task 和 Run 创建 |
| `src/agents/code_agent.py` | 代码任务接回 ExecutorDispatcher |
| `src/feishu_claude_automation/local_worker.py` | worktree、Claude、验证和交付 |
| `src/feishu_claude_automation/local_worker_client.py` | 本地/远程 Worker 协议 |
| `src/feishu_claude_automation/server.py` | 飞书 Inbox 和 Worker API |
| `src/feishu_claude_automation/policy.py` | 验证命令和控制权限 |
| `alembic/versions/002_reliable_worker.py` | 数据库升级 |
| `scripts/migrate_legacy_state.py` | JSON 一次性迁移 |
| `tests/test_reliable_worker.py` | 可靠性回归测试 |

