# 多 Agent 平台重构开发文档（大型重构）

> 版本：Draft v1　｜　适用仓库：`claude-feishu-automation`（`workAssist`）
> 目标读者：后端 / 平台 / 飞书应用开发
> 状态：设计评审用，落地前需 Review 定稿

---

## 0. TL;DR

把当前「飞书 → 改 GitHub 代码」的单通道流水线，重构为一个**可插拔的多 Agent 平台**：

- **Orchestra（项目经理）**：一个飞书机器人，负责与用户对话、澄清需求、拆解并派发任务、汇总回执、审批与审计。**只调度，不干活。**
- **职能 Agent**：每个 Agent 是**一个独立的飞书应用机器人**（独立身份、独立权限、独立 MCP/工具集）。例如 `CodeAgent`、`DocAgent`。
- **用户视角**：群里存在多个机器人。Orchestra 负责沟通；任务并行时，用户会看到 `@代码助手`、`@文档助手` 等多个机器人各自发言、各自回执。
- **执行后端可插拔**：GitHub Actions（代码）与飞书官方远程 MCP（文档）都收敛为 Executor 插件，走统一 Task 协议。
- **实现包名**：Python 包为 `agent_platform`（避免与标准库 `platform` 冲突）；目录/概念仍称「平台内核」。

本重构为 **breaking change 级别**，采用「新内核 + 旧链路兼容」双轨过渡，分 5 个里程碑落地。

> **进度（2026-07-13）**：分支 `refactor/agent-platform` 已启动；**M1 骨架已落地**（`agent_platform/` + PostgreSQL + `POST /v1/jobs` + CodeAgent 包装 + Alembic），旧 `/ai-fix` 兼容保留。

---

## 1. 背景与现状

### 1.1 现状架构

当前系统是飞书驱动的 GitHub 代码自动化编排器，全部逻辑集中在单包 `src/feishu_claude_automation/`。

```mermaid
flowchart LR
  user["飞书用户"] --> bot["单一飞书机器人"]
  bot --> orch["Orchestrator (单体)"]
  orch --> llm["Kimi 意图解析"]
  orch --> policy["策略引擎(仓库/分支)"]
  orch --> gh["GitHub workflow_dispatch"]
  gh --> runner["Actions + Claude Code"]
  runner --> pr["Pull Request"]
  runner --> cb["callback"]
  cb --> orch
  orch --> card["飞书卡片"]
```

### 1.2 关键模块（现状）

| 模块 | 文件 | 职责 |
|------|------|------|
| HTTP 入口 | `src/feishu_claude_automation/server.py` | `/feishu/events`、`/feishu/actions`、`/callbacks/runner`、`/tasks` |
| 编排核心 | `src/feishu_claude_automation/orchestrator.py` | 意图路由、会话状态机、任务创建、回调处理 |
| 意图解析 | `src/feishu_claude_automation/llm.py` | Kimi 意图分类 + mock 启发式 |
| 飞书客户端 | `src/feishu_claude_automation/feishu.py` | tenant token、发消息/卡片 |
| 策略 | `src/feishu_claude_automation/policy.py` | 仓库白名单、分支、风险审批、并发 |
| 数据模型 | `src/feishu_claude_automation/models.py` | `Task`、`TaskRequest`、`ConversationSession` |
| GitHub 调度 | `src/feishu_claude_automation/github.py` | workflow_dispatch |
| 存储 | `store.py` / `session_store.py` | JSON 文件存任务/会话 |
| 卡片 | `cards.py` | 交互卡片构造 |
| 审计 | `audit.py` | 审计日志 |

### 1.3 痛点与缺陷（相对目标）

| 维度 | 现状 | 缺陷 |
|------|------|------|
| 定位 | 「飞书改代码」流水线 | 非通用平台，加新能力会变成并列的半吊子分支 |
| 耦合 | 会话/意图/策略/执行全绑飞书 IM + GitHub Actions | 换入口/换执行后端成本高；飞书挂=整链路挂 |
| 编排 | 单任务状态机 | 无多 Agent 协作、无角色分工、无并行编排 |
| 执行 | Actions + Claude Code（只懂仓库文件） | 文档等能力塞不进同一 Runner，工具模型不一致 |
| 身份 | 一个飞书应用 + tenant token | 无法表达多 Agent 分身；UAT/TAT 无设计 |
| 策略 | 仓库/分支/风险关键词 | 无文档 ACL、无按 Agent 的工具白名单 |
| 运维 | 进程易停、错误笼统 `workflow failed` | 可观测性差，用户体感「机器人没回复」 |
| 意图 | 强绑 `repo/base_branch/prompt` | 自然语言一偏（`dev` vs `dev_test`）整单失败 |

---

## 2. 目标架构

### 2.1 三层模型

```mermaid
flowchart TB
  subgraph Channel["Channel 层（信道）"]
    fb_orch["飞书机器人: Orchestra(PM)"]
    fb_code["飞书机器人: CodeAgent"]
    fb_doc["飞书机器人: DocAgent"]
    web["Web / OpenAPI（可选）"]
  end

  subgraph Core["平台内核"]
    orchestra["Orchestra 编排服务\n澄清/拆解/派发/审批/汇总"]
    bus["任务总线 + 状态机"]
    registry["Agent 注册表\n(身份/能力/工具/策略)"]
    store["任务/会话/审计存储"]
  end

  subgraph Agents["Agent Runtime 层"]
    code["CodeAgent Executor\n(GitHub Actions)"]
    doc["DocAgent Executor\n(飞书远程 MCP)"]
    ext["...未来 Agent"]
  end

  fb_orch <--> orchestra
  web --> orchestra
  orchestra <--> bus
  orchestra --> registry
  orchestra <--> store
  bus --> code
  bus --> doc
  bus --> ext
  code -- 回执 --> bus
  doc -- 回执 --> bus
  code --> fb_code
  doc --> fb_doc
```

### 2.2 用户可见行为（多机器人）

- **入口**：用户主要 `@Orchestra`（项目经理）描述需求。
- **澄清**：Orchestra 追问缺失信息（仓库？文档链接？基线分支？）。
- **派发**：确认后，Orchestra 在群里 `@` 对应 Agent 机器人，或由对应 Agent 机器人主动在群里发「已接单」卡片。
- **并行**：多个任务同时进行时，`CodeAgent`、`DocAgent` 各自作为独立机器人发进度与结果，互不阻塞。
- **归属**：每条 Agent 消息都带 `task_id`，Orchestra 最终做一次汇总。

```mermaid
sequenceDiagram
  participant U as 用户
  participant O as Orchestra(机器人)
  participant C as CodeAgent(机器人)
  participant D as DocAgent(机器人)

  U->>O: 帮我改 workAssist 的登录逻辑，并把方案同步到需求文档
  O->>U: 澄清卡片（确认仓库/分支/文档链接）
  U->>O: 确认
  O->>C: 派发代码任务(task_code)
  O->>D: 派发文档任务(task_doc)
  par 并行执行
    C-->>U: [CodeAgent] 已接单 task_code，创建分支中…
    C-->>U: [CodeAgent] PR 已创建 ✅
  and
    D-->>U: [DocAgent] 已接单 task_doc，更新文档中…
    D-->>U: [DocAgent] 文档已更新 ✅
  end
  O->>U: 汇总：代码 PR + 文档链接，全部完成
```

---

## 3. 多机器人身份模型（核心设计点）

每个飞书机器人 = 一个飞书自建应用 = 一个 Agent 身份。

| 项 | Orchestra | CodeAgent | DocAgent |
|----|-----------|-----------|----------|
| 飞书 App | App-O | App-C | App-D |
| App ID/Secret | 独立 | 独立 | 独立 |
| verification/encrypt key | 独立 | 独立 | 独立 |
| 事件回调地址 | `/channels/feishu/orchestra/events` | `/channels/feishu/code/events` | `/channels/feishu/doc/events` |
| 权限 scope | `im:message*` | `im:message*` + GitHub Token | `im:message*` + `docx:*`/`drive:*`/`wiki:*` |
| 执行凭证 | 无（只对话） | `GITHUB_TOKEN` | 飞书 MCP **UAT（用户授权）** |
| MCP Allowed-Tools | 无 | 无（走 Actions） | `fetch-doc,update-doc,create-doc,search-doc` |

设计约定：

1. **每个机器人独立回调路径**，由 Channel 层按路径路由到对应 Agent 适配器。
2. **凭证隔离**：每个 App 的密钥独立存储、独立轮换，互不影响。
3. **同群协作**：所有机器人需被拉进同一群，或支持单聊转发。任务的 `chat_id` 在 Orchestra 与各 Agent 间传递，用于回执定位。
4. **消息署名**：每个 Agent 发消息带前缀/头像即天然区分（不同 App = 不同头像与名称）。
5. **DocAgent 采用 UAT（已定稿）**：文档读写以**用户身份**授权，代表「谁让我改，就以谁的权限改」。需实现 OAuth 授权与 `user_access_token` 刷新；用户首次使用文档能力时，DocAgent 引导完成授权（详见 §3.1）。

> ⚠️ 运维成本：N 个机器人 = N 套飞书应用配置、N 组密钥、N 个事件订阅。需在飞书后台逐个创建、逐个授权、逐个上线审批。这是本方案相对「单机器人」最大的额外成本，需接受。

### 3.1 DocAgent UAT 授权流程（已定稿）

选择 UAT 而非 TAT 的理由：

- `search-doc` **仅支持 UAT**，首批要覆盖「搜我的文档再改」。
- 以用户身份操作，天然继承该用户对文档的读写 ACL，无需为应用逐份文档「添加文档应用」。
- 审计更清晰：操作即该用户本人行为。

代价：需实现并维护 OAuth 授权链路与 token 生命周期。

```mermaid
sequenceDiagram
  participant U as 用户
  participant D as DocAgent(机器人)
  participant F as 飞书 OAuth
  participant S as 平台(token 存储)

  U->>D: 需要改文档（首次）
  D->>U: 尚未授权，发送授权卡片(授权链接)
  U->>F: 点击授权，登录并同意 scope
  F-->>S: 回调 code
  S->>F: code 换 user_access_token + refresh_token
  S-->>D: 授权完成
  D-->>U: 已授权，继续执行文档任务
  Note over S: refresh_token 到期前自动刷新<br/>UAT 加密存储，按 requester_id 归属
```

端点与协议（遵循 OAuth 2.0 / RFC 6749，可用标准 OAuth 客户端库）：

| 步骤 | 端点 | 说明 |
|------|------|------|
| 1. 拿授权码 | `GET https://accounts.feishu.cn/open-apis/authen/v1/authorize` | 参数 `client_id`、`response_type=code`、`redirect_uri`、`scope`、`state`；授权码有效期 **5 分钟、仅用一次** |
| 2. 换 token | `POST https://open.feishu.cn/open-apis/authen/v2/oauth/token` | `grant_type=authorization_code` + `client_id/secret/code/redirect_uri`；返回 `access_token`(≈2h)、`refresh_token`、各自 `expires_in` |
| 3. 刷新 | 同 token 端点 | `grant_type=refresh_token`；**旧 refresh_token 用一次即失效**，需存新返回的 refresh_token |

要点（实现清单）：

1. **必须申请 `offline_access`**：否则响应**不返回 `refresh_token`**，用户每 2 小时就要重新授权。
2. **必须在开发者后台配置重定向 URL**：`开发配置 → 安全设置 → 重定向 URL`，且与请求里的 `redirect_uri` **完全一致**（否则报 20071）；须为**公网 HTTPS**。
3. **必须先申请文档 scope 再拼接**：后台「权限管理」开通 `docx:document*`、`drive:*`、`wiki:*`、`search:docs:read` 等，未开通则用户授权时报 20027；单次最多 200 个 scope。
4. **用户需有应用使用权限**：否则换 token 报 20010（发布版本 + 可用范围包含该用户）。
5. **按用户存储**：`access_token`/`refresh_token` 以 `requester_id`（`open_id`）为键**加密**存储于 PostgreSQL（预留 ≥4KB/字段）；用 `expires_in` 动态判过期，**不要硬编码有效期**。
6. **自动刷新 + 轮换**：调用 MCP 前检查过期；刷新后**覆盖保存新的 refresh_token**；刷新失败（过期/被撤销）则重新引导授权。
7. **`state` 防 CSRF**：授权发起时带 `state`，回调校验，并借此把 code 关联回具体用户与会话。
8. **回调端点**：`/channels/feishu/doc/oauth/callback` 接收 `code` + `state` → 换 token → 落库 → 回执用户。
9. **调用**：MCP 请求头用 `X-Lark-MCP-UAT: <access_token>`。
10. **兜底**：用户拒绝或未授权时，DocAgent 引导授权，不阻塞其它 Agent。
11. **（可选）PKCE**：v2/v3 支持；用 v3 token 端点（`https://accounts.feishu.cn/oauth/v3/token`）时 PKCE 校验更严格，如启用需成对传 `code_challenge`/`code_verifier`。

---

## 4. 核心概念与数据模型

### 4.1 概念

| 概念 | 说明 |
|------|------|
| `Channel` | 信道适配器（飞书某机器人 / Web / API），负责收发与身份换算 |
| `Agent` | 一个职能单元：身份 + 能力声明 + 工具集 + 策略 |
| `Job` | 用户一次意图对应的顶层工作（可拆为多个 Task） |
| `Task` | 派给单个 Agent 的可执行单元 |
| `Executor` | Task 的执行后端插件（Actions / MCP / 未来） |
| `Approval` | 高风险操作的人工确认 |

### 4.2 新数据模型（提议）

在 `models.py` 基础上新增（不删旧，先并存）：

```python
# platform/models.py（新增包）
from dataclasses import dataclass, field
from enum import Enum

class AgentKind(str, Enum):
    CODE = "code"
    DOC = "doc"

class JobStatus(str, Enum):
    RECEIVED = "received"
    CLARIFYING = "clarifying"
    PLANNED = "planned"
    RUNNING = "running"
    PARTIAL = "partial"      # 部分子任务成功
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class AgentSpec:
    id: str                      # "code" / "doc"
    kind: AgentKind
    display_name: str            # "代码助手"
    feishu_app_id: str
    executor: str                # "github_actions" / "feishu_mcp"
    allowed_tools: list[str] = field(default_factory=list)
    policy_ref: str = ""

@dataclass
class PlatformTask:
    id: str
    job_id: str
    agent_id: str                # 派给哪个 Agent
    goal: str                    # 自然语言目标
    inputs: dict                 # 结构化输入（repo/branch 或 doc_url…）
    status: str
    result: dict = field(default_factory=dict)
    error: str = ""
    chat_id: str = ""            # 回执定位
    requester_id: str = ""
    created_at: str = ""
    updated_at: str = ""

@dataclass
class Job:
    id: str
    requester_id: str
    chat_id: str
    goal: str
    status: str
    task_ids: list[str] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
```

> 兼容策略：现有 `Task/TaskRequest` 保留，`CodeAgent` Executor 内部把 `PlatformTask` 翻译成现有 `TaskRequest`，复用 `github.py`。

### 4.3 存储与检索架构决策（PostgreSQL / 不启用 RAG）

> 决策（已定稿 2026-07-13）：**主存储直接采用 PostgreSQL**（不经 SQLite 过渡）；**不启用 RAG / 向量库**（含 pgvector），文档检索完全依赖飞书官方 MCP。

#### 4.3.1 直接上 PostgreSQL

平台核心数据是**结构化、事务性、并发写**的：`Job`、`PlatformTask`、`Session`、`Approval`、`Audit`、`AgentSpec`、以及 DocAgent 的 **UAT token（按用户）**。诉求：

- 事务与一致性（一个 Job 拆多个 Task，状态并发流转）
- 精确查询与外键（按 `job_id`/`requester_id`/`status` 检索）
- 唯一约束与幂等（`task_id` 防重复派发）
- 加密字段安全存储（`refresh_token` / `user_access_token`）

落地约定：

- **从 M1 起即用 PostgreSQL**，不做 SQLite 过渡，避免二次迁移与方言差异。
- ORM/迁移：SQLAlchemy + Alembic（或等价）管理 schema 版本。
- 连接池 + 事务；关键表加唯一约束（`task_id`）与索引（`job_id`/`requester_id`/`status`）。
- 敏感字段（UAT/refresh_token）**应用层加密**后入库（见 §10）。
- 本地/测试可用 Docker 起 PostgreSQL；CI 用临时实例。

核心表（示意）：

| 表 | 关键字段 | 说明 |
|----|----------|------|
| `jobs` | `id`, `requester_id`, `chat_id`, `status`, `plan(jsonb)` | 顶层工作 |
| `tasks` | `id`(uniq), `job_id`(fk), `agent_id`, `status`, `inputs(jsonb)`, `result(jsonb)` | 子任务 |
| `sessions` | `id`, `chat_id`, `requester_id`, `status`, `messages(jsonb)` | 会话 |
| `approvals` | `id`, `task_id`(fk), `approver`, `decision`, `ts` | 审批 |
| `audit_logs` | `id`, `job_id`, `task_id`, `agent_id`, `event`, `payload(jsonb)`, `ts` | 审计 |
| `user_oauth` | `requester_id`(uniq), `agent_id`, `access_token(enc)`, `refresh_token(enc)`, `expires_at`, `scope` | UAT 令牌 |

#### 4.3.2 不启用 RAG / 向量库

**决策：本平台不自建 RAG、不引入向量库（包括 pgvector）。** 依据：

1. **飞书官方 `search-doc`（UAT）已提供文档检索**，DocAgent 直接调 MCP 即可，无需自建索引/embedding/同步管道。
2. `fetch-doc` 按需取指定文档全文（支持分段）喂给模型，**实时且继承用户 ACL**，比预建向量索引更安全简单。
3. 省去切块、embedding、增量同步、跨用户权限过滤等一整套系统，降低复杂度与成本。

> 影响：文档「搜索 + 读取 + 修改」全部走飞书 MCP（`search-doc`/`fetch-doc`/`update-doc`）。若未来出现「跨历史任务/私有知识语义问答」等飞书搜不到的需求，再单独立项评审，不在本重构范围。

---

## 5. 目标目录结构

```text
src/
  agent_platform/               # 新内核（包名避开 stdlib platform）
    __init__.py
    models.py                   # Job / PlatformTask / AgentSpec
    orchestra.py                # 项目经理：澄清/拆解/派发/汇总
    planner.py                  # LLM/启发式拆解：goal -> [PlatformTask]
    registry.py                 # Agent 注册表（读 config/agents.json）
    bus.py                      # 任务分发 + 状态流转
    store.py                    # Job/Task/审计存储（PostgreSQL 后端）
    db.py                       # SQLAlchemy 引擎/会话
    app.py                      # PlatformApp 组装
    errors.py

  channels/                     # 信道层
    __init__.py
    base.py                     # Channel 抽象
    feishu_channel.py           # 多机器人：按路径路由到 Agent（M3）

  agents/                       # Agent Runtime
    __init__.py
    base.py                     # Agent/Executor 抽象接口
    code_agent.py               # 封装现有 GitHub 流程（M1）
    doc_agent.py                # 飞书远程 MCP（M2 stub）

  feishu_claude_automation/     # 旧包：过渡期保留，逐步收敛
    server.py                   # 兼容旧路由 + 挂载 POST /v1/jobs

config/
  agents.json                   # Agent/机器人清单与能力
  policy.example.json

alembic/                        # PostgreSQL 迁移
alembic.ini
```


---

## 6. 接口契约

### 6.1 Executor 抽象

```python
# agents/base.py
from typing import Protocol
from platform.models import PlatformTask

class Executor(Protocol):
    def can_handle(self, task: PlatformTask) -> bool: ...
    def dispatch(self, task: PlatformTask) -> None:
        """异步触发执行；结果通过回调写回 bus。"""
    def on_callback(self, payload: dict) -> PlatformTask:
        """执行后端回调 -> 更新 task 状态。"""
```

- `CodeAgent` 的 `dispatch` = 现有 `github.dispatch_workflow`；`on_callback` = 现有 `/callbacks/runner`。
- `DocAgent` 的 `dispatch` = 调 `mcp.feishu.cn`（同步返回或短轮询），`on_callback` 可内联。

### 6.2 Agent 注册表（`config/agents.json`）

```json
{
  "agents": [
    {
      "id": "code",
      "kind": "code",
      "display_name": "代码助手",
      "feishu_app_id_env": "FEISHU_CODE_APP_ID",
      "executor": "github_actions",
      "policy_ref": "code_policy"
    },
    {
      "id": "doc",
      "kind": "doc",
      "display_name": "文档助手",
      "feishu_app_id_env": "FEISHU_DOC_APP_ID",
      "executor": "feishu_mcp",
      "allowed_tools": ["fetch-doc", "update-doc", "create-doc", "search-doc"],
      "policy_ref": "doc_policy"
    }
  ]
}
```

### 6.3 平台 HTTP 端点（新增）

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/jobs` | POST | 创建 Job（脱离飞书也能用，便于测试） |
| `/v1/jobs/{id}` | GET | 查询 Job 及子任务 |
| `/v1/tasks/{id}/callback` | POST | Executor 回调统一入口 |
| `/channels/feishu/{agent}/events` | POST | 各机器人事件（`orchestra`/`code`/`doc`） |
| `/channels/feishu/{agent}/actions` | POST | 各机器人卡片回调 |
| `/health` | GET | 健康检查 |

### 6.4 飞书远程 MCP 调用（DocAgent）

- Endpoint：`POST https://mcp.feishu.cn/mcp`
- Header：`X-Lark-MCP-UAT`（用户身份，已定稿）、`Content-Type: application/json`、`X-Lark-MCP-Allowed-Tools: fetch-doc,update-doc,create-doc,search-doc`
- 协议：JSON-RPC 2.0，方法 `initialize` → `tools/list` → `tools/call`。
- 前置：先完成 §3.1 的 UAT 授权拿到 `user_access_token`；`search-doc` 依赖 UAT，本方案已满足。

---

## 7. Orchestra 编排流程

```mermaid
stateDiagram-v2
    [*] --> Received
    Received --> Clarifying: 信息不足
    Clarifying --> Clarifying: 追问/补充
    Clarifying --> Planned: 信息齐全
    Received --> Planned: 一次说清
    Planned --> AwaitingApproval: 含高风险
    Planned --> Running: 直接执行
    AwaitingApproval --> Running: 用户确认
    AwaitingApproval --> Cancelled: 拒绝
    Running --> Partial: 部分子任务失败
    Running --> Done: 全部成功
    Partial --> Running: 重试失败子任务
    Partial --> Done: 补齐
    Running --> Failed: 全部失败
    Done --> [*]
    Failed --> [*]
    Cancelled --> [*]
```

Orchestra 职责边界：

- **它做**：与用户对话、澄清、调用 `planner` 拆解、按 registry 选 Agent、发审批、聚合回执。
- **它不做**：不直接改代码/文档；不持有 GitHub/MCP 执行凭证。

---

## 8. 现有代码迁移映射

| 现有 | 去向 | 处理 |
|------|------|------|
| `server.py` 路由 | `server.py` + `channels/feishu_channel.py` | 拆成多机器人路径路由 |
| `orchestrator.handle_feishu_message` | `platform/orchestra.py` | 意图→拆解→派发 |
| `orchestrator.handle_runner_callback` | `agents/code_agent.on_callback` | 收敛为 Executor 回调 |
| `llm.py`（意图） | `platform/planner.py` | 从「单任务意图」升级为「多任务拆解」 |
| `github.py` | `agents/code_agent.py` | 作为 CodeAgent 内部实现 |
| `feishu.py` | `channels/feishu_channel.py` | 支持多 App token（按 agent 取凭证） |
| `policy.py` | `platform/` + 按 agent 策略 | 扩展 doc ACL / 工具白名单 |
| `models.py` | 保留 + `platform/models.py` | 双模型并存过渡 |
| `store.py`/`session_store.py` | `platform/store.py`（DB 后端） | 从 JSON 迁移到关系型 DB，加 Job 维度（§4.3） |

关键复用点（现有代码可直接落到 CodeAgent）：

```62:107:src/feishu_claude_automation/models.py
@dataclass
class Task:
    id: str
    repo: str
    prompt: str
    base_branch: str
    work_branch: str
    ...
    @classmethod
    def from_request(cls, request: TaskRequest, work_branch: str, risk_level: RiskLevel) -> Task:
        ...
```

---

## 9. 分阶段实施计划（里程碑）

### M0　设计定稿与飞书应用准备（1 周）
- 定稿本文档、身份模型、Agent 清单。
- 飞书后台创建 3 个自建应用（Orchestra/Code/Doc），配置事件订阅与权限。
- DocAgent 权限（UAT scope）：`docx:document*`、`drive:*`、`wiki:*`、`search:docs:read`；配置 OAuth 重定向地址。
- 预置 **PostgreSQL** 实例与迁移工具（Alembic），建库建表（§4.3）。
- **验收**：3 个机器人都能被拉群、能收到 `im.message.receive_v1`（先回声测试）；PostgreSQL 连通、迁移可执行。

### M1　平台内核骨架 + PostgreSQL + 脱飞书可测（2 周）
- 新建 `platform/`、`agents/`、`channels/` 包与抽象接口。
- 实现 `POST /v1/jobs`、`registry`、`bus`、`store`（**PostgreSQL** 后端，§4.3）。
- CodeAgent Executor 包装现有 GitHub 流程（复用 `github.py`）。
- **验收**：不经飞书，`curl POST /v1/jobs` 能派 CodeAgent 跑通现有代码 PR 流程，Job/Task/审计写入 PostgreSQL。

### M2　DocAgent + MCP + UAT 授权（2 周）
- 实现 `agents/mcp_client.py`（JSON-RPC）+ `doc_agent.py`。
- **UAT OAuth 链路**：授权卡片、`/channels/feishu/doc/oauth/callback`、token 加密存储与自动刷新（§3.1）。
- 支持 `fetch-doc` / `update-doc` / `search-doc`；写前确认卡片 + 写前 `fetch-doc` 快照。
- **验收**：用户完成授权后，`curl POST /v1/jobs`（doc 目标）能搜索、读、改用户自己的文档，token 到期自动刷新，审计落库。

### M3　多机器人 Channel + Orchestra 编排（2 周）
- `feishu_channel.py` 多 App 路由 + 多 token 管理。
- `orchestra.py` + `planner.py`：澄清、拆解、派发、并行、汇总。
- 各 Agent 以独立机器人身份在群里发回执。
- **验收**：群里 `@Orchestra` 一句话，触发 Code+Doc 并行，两个机器人各自回执，Orchestra 汇总。

### M4　治理、迁移收尾、灰度（1.5 周）
- 按 agent 的策略/审批/配额；密钥轮换。
- 旧 `/feishu/events`（单机器人 `/ai-fix`）**长期保留为兼容层**（已定稿），封装为一个内置 Channel 直连 CodeAgent，不下线。
- systemd 保活、可观测性、告警。
- **验收**：灰度用户跑通；旧 `/ai-fix` 链路继续可用且回归通过；回滚预案演练通过。

> 总量约 8–9 周（单人估算，含 UAT 授权、DB 迁移、联调与飞书审批等待）。

---

## 10. 权限与安全

| 项 | 要求 |
|----|------|
| 凭证隔离 | 每机器人独立密钥，最小权限；集中在 `.env`/密管，不入库 |
| 写操作确认 | 文档 `update-doc`、高风险代码操作前必须用户确认卡片 |
| 文档 ACL | DocAgent 只能操作白名单/已授权文档；越权直接拒绝并提示授权路径 |
| 审计 | 记录：谁(requester)、哪个 Agent、什么操作、目标(repo/doc)、依据哪次确认、结果 |
| 幂等 | Executor 回调按 `task_id` 幂等，防重复 PR/重复改文档 |
| 回滚 | 文档写操作保留原内容快照（`fetch-doc` 先存），便于人工回退 |

---

## 11. 可观测性与运维

- **结构化日志**：每条带 `job_id`/`task_id`/`agent_id`。
- **错误细化**：替换笼统 `workflow failed`，回传具体原因（如 checkout 分支不存在）到飞书卡片。
- **健康检查**：`/health` 汇报各 Channel token 状态、各 Executor 可用性。
- **保活**：Orchestra 与各 Agent 服务用 systemd/supervisor，异常自拉起。
- **限流与成本**：LLM/MCP 调用计数与配额；飞书/GitHub API 频控退避。
- **回执合并折叠（已定稿）**：各 Agent 仍以独立机器人身份发言，但进度更新采用**可折叠卡片 + 原地更新**（同一张卡片刷新状态，而非每步新发一条），Orchestra 在 Job 结束时发一张**统一汇总卡片**（含各子任务结果与链接），降低群内刷屏。

---

## 12. 测试策略

| 层 | 测试 |
|----|------|
| 单元 | planner 拆解、registry 选择、policy 校验 |
| 契约 | Executor 接口契约测试（Code/Doc 各一套 mock） |
| 集成 | `POST /v1/jobs` 全链路（MCP/Actions 用 mock server） |
| 端到端 | 灰度群：多机器人并行回执与汇总 |
| 回归 | 旧 `/ai-fix` 兼容用例继续通过 |

现有测试（`tests/test_orchestrator*.py`）在过渡期继续跑，保障 CodeAgent 行为不回退。

---

## 13. 风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| 多飞书应用运维复杂、审批慢 | 高 | M0 提前申请；用配置化 registry 降低接入成本 |
| 大重构中断现有业务 | 高 | 双轨并行，旧 `/ai-fix` 链路长期保留兼容（已定稿） |
| UAT token 泄露/过期 | 高 | 加密存储、按用户隔离、自动刷新、刷新失败重新授权 |
| 文档写操作误改不可逆 | 中 | 先 `fetch-doc` 快照 + 写前确认 + 审计 |
| UAT 授权转化率低（用户不授权） | 中 | 引导卡片说明用途；未授权仅禁用 doc 能力，不影响其它 Agent |
| DB 并发写/迁移问题 | 中 | M1 起直接 PostgreSQL；Alembic 管理 schema；唯一约束 + 事务保障幂等 |
| LLM 拆解不稳定 | 中 | 拆解结果走确认卡片；关键字段可结构化兜底 |
| 群里多机器人刷屏 | 低 | 回执合并折叠卡片，Orchestra 统一汇总（已定稿，§11） |

---

## 14. 回滚预案

- 平台内核以 feature flag 控制；异常时 Channel 切回旧 `feishu_claude_automation` 单机器人链路。
- 各机器人可单独下线（在飞书后台停用对应 App），不影响其余 Agent。
- 数据层：迁移期用迁移脚本把旧 JSON（`data/tasks.json`）导入 DB；DB 为唯一事实来源，保留 JSON 只读备份用于回退。

---

## 15. 附录

### 15.1 飞书多机器人配置清单（每个 App 重复）

1. 开放平台创建自建应用，获取 `App ID/Secret`。
2. 事件订阅：填对应回调 `/channels/feishu/{agent}/events`，订阅 `im.message.receive_v1`。
3. 卡片回调：`/channels/feishu/{agent}/actions`。
4. 权限：Orchestra/Code/Doc 按第 3 节授予。
5. DocAgent（UAT）：开启「网页授权」/ OAuth，配置重定向地址；申请 `docx:document*`、`drive:*`、`wiki:*`、`search:docs:read` 等 scope。用户首次使用时经授权卡片完成 OAuth（无需逐份文档「添加文档应用」，以用户 ACL 为准）。
6. 发布版本、拉入协作群。

### 15.2 环境变量（新增，示例）

```bash
# Orchestra
FEISHU_ORCH_APP_ID=cli_xxx
FEISHU_ORCH_APP_SECRET=xxx
FEISHU_ORCH_VERIFICATION_TOKEN=xxx

# CodeAgent
FEISHU_CODE_APP_ID=cli_xxx
FEISHU_CODE_APP_SECRET=xxx
GITHUB_TOKEN=ghp_xxx

# DocAgent（UAT / 用户授权）
FEISHU_DOC_APP_ID=cli_xxx
FEISHU_DOC_APP_SECRET=xxx
FEISHU_DOC_OAUTH_REDIRECT_URL=https://<host>/channels/feishu/doc/oauth/callback  # 须与后台重定向 URL 一致，公网 HTTPS
FEISHU_DOC_OAUTH_SCOPES="offline_access docx:document drive:drive wiki:wiki search:docs:read"
FEISHU_MCP_ALLOWED_TOOLS=fetch-doc,update-doc,create-doc,search-doc
# user_access_token / refresh_token 按用户加密存储于 PostgreSQL，不放 .env

# 存储与加密
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/agent_platform
TOKEN_ENC_KEY=<32B base64>   # 加密 UAT/refresh_token 的对称密钥（建议走 KMS）
```

### 15.3 飞书远程 MCP 最小调用示例

```bash
curl -sS -X POST https://mcp.feishu.cn/mcp \
  -H "Content-Type: application/json" \
  -H "X-Lark-MCP-UAT: $USER_ACCESS_TOKEN" \
  -H "X-Lark-MCP-Allowed-Tools: fetch-doc,update-doc,search-doc" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"fetch-doc","arguments":{"docID":"<doc_token>"}}}'
```

### 15.4 决策记录（已定稿 2026-07-13）

| # | 决策项 | 结论 | 影响章节 |
|---|--------|------|----------|
| 1 | 首批 Agent 范围 | **只上 Code + Doc**，其余排期 | §2、§9 |
| 2 | DocAgent 身份 | **UAT（用户授权）**，实现 OAuth + token 刷新 | §3.1、§6.4、§9 M2 |
| 3 | 旧 `/ai-fix` 链路 | **长期保留兼容**，封装为内置 Channel 直连 CodeAgent | §9 M4、§13、§14 |
| 4 | 存储层 | **直接采用 PostgreSQL**（M1 起，不经 SQLite）；**不启用 RAG/向量库**（含 pgvector） | §4.3 |
| 5 | 并行回执 | **合并折叠**：可折叠卡片 + 原地更新 + Orchestra 统一汇总 | §11、§13 |

#### 4 号决策要点：直接 PG、不启用 RAG

- 平台核心数据是结构化/事务性/并发写（Job、Task、Session、Audit、UAT token）→ **直接上 PostgreSQL**，M1 即用，不做 SQLite 过渡以免二次迁移。
- 文档检索/读取完全走飞书官方 **`search-doc` / `fetch-doc`（UAT）**，**不自建 RAG、不引入向量库**。
- 若未来出现飞书搜不到的「历史任务/私有知识语义问答」需求，另立项评审，不在本重构范围。

#### 遗留待定（实施时细化）

1. Alembic 迁移脚本 owner 与 CI 集成方式。
2. UAT 授权的 scope 最终清单与安全审计口径。
3. UAT/refresh_token 的应用层加密方案（KMS / 对称密钥）。
