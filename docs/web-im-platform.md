# 多智能体 IM 平台开发文档（脱离飞书 · 自建前端）

> 版本：Draft v1　｜　适用仓库：`claude-feishu-automation`（`workAssist`）
> 目标读者：本人学习 + 面试准备
> 状态：计划已评审，待落地
> 前置文档：[refactor-agent-platform.md](refactor-agent-platform.md)（M1 骨架已落地，本文档是其延续）

---

## 0. TL;DR

在现有「平台内核（`agent_platform`）+ 飞书通道」的基础上，**新增一个自建的 Web IM 通道**：

- **前端**：React + Vite + TS 的聊天界面，多 Agent 以"虚拟成员"身份出现在会话中
- **实时通道**：WebSocket 推送任务状态（替代飞书的消息推送能力）
- **交互**：自然语言对话式 —— LLM 解析意图 → 任务预览 → 用户确认 → 执行（human-in-the-loop）
- **用户体系**：多用户注册登录，JWT 鉴权
- **飞书通道保留**：老 `server.py` 一行不动，新旧双跑，平滑过渡

分 6 个里程碑（M1–M6）落地，预计后端新增 1500–2500 行 + 一个前端工程。

---

## 1. 背景：为什么要脱离飞书

### 1.1 现状的痛点

1. **可观测性差**：任务状态完全依赖飞书消息通知。orchestrator 假死时，任务无声丢失（2026-07 实际发生：飞书任务未执行无返回，排查发现远程 orchestrator 挂起、本地 worker 未运行、无持久化日志）
2. **平台绑定**：身份、会话、交互（卡片按钮）、报告投递全部绑死飞书，换一个 IM 就要重写
3. **交互受限**：任务进度无法实时展示，只能靠"状态跃迁时发一条消息"

### 1.2 已具备的解耦基础（重构 M1 的成果）

| 模块 | 路径 | 飞书耦合度 |
|---|---|---|
| 数据模型 Job/PlatformTask/AgentSpec | `src/agent_platform/models.py` | 零（`chat_id`/`requester_id` 是不透明字符串） |
| SQLAlchemy 持久化 | `src/agent_platform/store.py` + `db.py` | 零 |
| Agent 注册表 | `src/agent_platform/registry.py` + `config/agents.json` | 仅一个 `feishu_app_id_env` 字段 |
| 任务规划（goal → agent 路由） | `src/agent_platform/planner.py` | 零 |
| 任务总线（分发/回调/状态汇总） | `src/agent_platform/bus.py` | 零 |
| 编排器 | `src/agent_platform/orchestra.py` | 零 |
| 执行器（代码/文档） | `src/agents/` | 零 |
| 通道协议 | `src/channels/base.py` | 零 —— 就是为扩展预留的接口 |
| 平台无关 HTTP 接口 | `server.py` 中 `/v1/jobs`、`/v1/tasks/callback`、`/v1/worker/jobs/*` | 零 |

**核心复用度约 70%**：任务生命周期、多 agent 路由、执行器、worker 链路、审计全部不动。

### 1.3 仍然绑死飞书、需要替代的部分

| 飞书能力 | 现状位置 | 替代方案 |
|---|---|---|
| 消息入口（事件 webhook） | `orchestrator.py` `handle_feishu_message` | 前端 REST POST + WebSocket |
| 用户身份/鉴权 | 飞书 open_id + verification token | 自建用户表 + JWT |
| 消息历史 | 存在飞书侧 | 自建 Message 表 |
| 卡片确认/取消按钮 | `cards.py` + 文本猜测（`looks_like_confirmation`） | 前端按钮 + WS 结构化 action |
| 报告投递云文档 | `feishu_docs.py` | 前端渲染 markdown（回调本带 `report_markdown`） |
| 多 bot 人格（M3） | 需多个飞书应用 | 前端聊天界面里的虚拟成员，零成本 |
| 推送到用户屏幕 | 飞书 App 长连接（免费获得） | **自建 WebSocket——本项目核心新增** |

---

## 2. 两个核心概念（设计决策的依据）

### 2.1 回调（Webhook）不会消失

回调是"异步任务 + 分布式执行"带来的，不是飞书带来的：

- **飞书事件回调（第 1 层）**：飞书 → orchestrator，自建平台后**删除**
- **worker 结果回调（第 2 层）**：worker（跑在本地 Mac）→ orchestrator（云端），**保留**

worker 执行是分钟级的异步任务，orchestrator 与 worker 是两个进程、两台机器。任务完成时通知 orchestrator 的候选方案只有三种：

| 方案 | 可行性 |
|---|---|
| HTTP 回调（现状，`local_worker.py` `_callback`） | ✅ 保留 |
| worker 直写数据库 | ❌ DB 在云端内网，本地 Mac 不可达 |
| orchestrator 轮询 worker | ❌ worker 没有 HTTP 服务端 |

### 2.2 WebSocket 的原理与选型

浏览器没有公网地址、不监听端口，服务器**无法主动发起连接到浏览器**。WebSocket 的解法：

1. 浏览器先发起带 `Upgrade: websocket` 头的 HTTP 请求
2. 服务器返回 `101 Switching Protocols`，这条 TCP 连接"原地升级"，不再断开
3. 之后双向随时可写：服务器 `send()` → 浏览器 `onmessage` 触发

类比：浏览器主动打电话给服务器，双方都不挂电话，之后谁都可以随时说话。

选型对比：

| 方案 | 适用 | 结论 |
|---|---|---|
| 轮询 | 最简单，原型期可用 | 前期兜底，前端刷新/重连后拉历史 |
| SSE | 服务器→浏览器单向推送 | 够用但双向交互还要另走 REST |
| **WebSocket** | 双向：用户发消息 + 服务器推状态走同一连接 | ✅ **本项目采用**（用户明确选择，且面试价值更高） |

---

## 3. 目标架构

```
┌──────────────┐  REST + WebSocket  ┌─────────────────────────────────┐
│  React 前端   │ ◀════════════════▶ │  FastAPI 新服务 (src/web_api/)   │
│  (Vite + TS)  │                    │   ├─ auth 模块 (JWT + bcrypt)   │
└──────────────┘                    │   ├─ conversations / messages   │
                                    │   ├─ /v1/jobs（平台路由平移）    │
                                    │   ├─ /ws/{conv_id}（推送通道）   │
                                    │   └─ /callbacks（worker 回调）   │
                                    └───────┬────────────┬───────────┘
                                            │            │ 状态变更时广播
                                     Postgres/SQLite     ▼
                              (jobs/tasks/users/    TaskBus（不动）
                               conversations/msgs)    │
                                            ┌─────────┼──────────┐
                                            ▼         ▼          ▼
                                       code_agent  doc_agent  local_worker
                                                              (本地 Mac)
```

### 3.1 关键设计决策

**决策 1：新建独立 FastAPI 应用，老 `server.py` 不动**
- 飞书通道继续跑老服务，Web 通道跑新服务，双跑互不影响
- 现有 `server.py` 基于 stdlib `http.server`，同步阻塞、不支持 WebSocket，必须换框架

**决策 2：`chat_id` 字段直接复用为 `conversation_id`**
- `agent_platform` 层的 Job/Task 本就用 `chat_id` 做会话关联，平台内核零改动

**决策 3：广播点放在 FastAPI 回调路由 handler，不侵入 `bus.py`**
- worker 的状态回调本来就经由 HTTP 路由进来（`/callbacks/runner`、`/v1/tasks/callback`）
- 路由 handler 里更新完状态后直接调 `ConnectionManager.broadcast`，bus 保持纯粹、可测试

**决策 4：先落库、再广播（至少一次送达）**
- 所有消息/状态先写 DB 再推 WS；推送丢了用户刷新也能从 DB 拉到
- WS 只负责"实时性"，DB 才是真相来源

**决策 5：同步 store 在 FastAPI 中用 `def` 路由**
- FastAPI 会把 `def` 路由自动丢进线程池执行，避免同步 SQLAlchemy 阻塞事件循环
- WS handler（必须 async）里调同步代码时用 `asyncio.to_thread`

---

## 4. 里程碑

### M1 — FastAPI 骨架 + 平台路由平移

新增 `src/web_api/` 包：

| 文件 | 职责 |
|---|---|
| `web_api/app.py` | FastAPI 实例、路由挂载、uvicorn 入口 |
| `web_api/deps.py` | 共享依赖（settings、store、bus 的构建，复用 `agent_platform.app.build_platform_app`） |

平移路由（逻辑从 `server.py` 提取，行为不变）：

- `POST /v1/jobs` — 创建并派发任务
- `GET /v1/jobs/{job_id}` — 查询任务包
- `POST /v1/tasks/callback` — 平台任务回调
- `POST /v1/worker/jobs/claim` / `POST /v1/worker/jobs/complete` — worker 认领/完成（保留 `LOCAL_WORKER_TOKEN` Bearer 鉴权）

新依赖：`fastapi`、`uvicorn[standard]`、`PyJWT`、`bcrypt`

> 📚 **学习点 ①**：同步 SQLAlchemy 在 async 框架中的处理——`def` 路由进线程池。面试高频：async 框架里混用同步 IO 的标准解法。

### M2 — 用户认证（JWT + bcrypt）

- `db.py` 新增 `UserRow`（id、username、password_hash、created_at）
- `web_api/auth.py`：
  - `POST /auth/register`、`POST /auth/login`（返回 JWT）
  - bcrypt 哈希（慢哈希 + 盐）
  - `get_current_user` 依赖注入（校验 `Authorization: Bearer <jwt>`）

> 📚 **学习点 ②**：bcrypt 为什么抗彩虹表（慢哈希 + 每密码独立盐）；JWT 三段结构（header.payload.signature）与无状态鉴权的取舍（无法主动失效 → 短过期 + refresh）
> 📚 **学习点 ③**：FastAPI `Depends` 依赖注入——鉴权与业务解耦，测试时可替换为 mock

### M3 — 会话/消息持久化 + WebSocket 推送

- `db.py` 新增 `ConversationRow`、`MessageRow`
- REST：
  - `POST /conversations`（创建会话）、`GET /conversations`（我的会话列表）
  - `GET /conversations/{id}/messages`（历史消息，分页）
- `web_api/ws.py`：
  - `ConnectionManager`：`conversation_id → set[WebSocket]`
  - `WS /ws/{conversation_id}?token=...`：建连鉴权、心跳 ping/pong、断线清理
- 回调路由 handler 中：更新状态 → 落库一条状态消息 → `broadcast`

> 📚 **学习点 ④**：WS 握手升级（101）、为什么服务器不能主动连浏览器
> 📚 **学习点 ⑤**：先落库后广播的"至少一次送达"；回调幂等（worker 重试导致重复回调如何去重）

### M4 — 自然语言对话流

- 复用 `feishu_claude_automation/llm.py` 的 `interpret()`（意图解析，351 行，与飞书无耦合）
- 流程：用户消息 → LLM 解析意图 → 落库一条"任务预览"消息（含 repo/branch/prompt）→ 前端确认按钮 → WS action 消息 → `orchestra.create_and_dispatch`
- 确认/取消从"文本猜测"（`looks_like_confirmation`）改为 WS 结构化 action：
  ```json
  {"type": "action", "action": "confirm" | "cancel", "ref_message_id": "..."}
  ```
- `ConversationSession` 状态机（CLARIFYING → AWAITING_CONFIRM → RUNNING）挂到 conversation 上

> 📚 **学习点 ⑥**：状态机驱动对话——LLM 输出不直接执行，落到状态机由用户确认，是 human-in-the-loop 的标准模式

### M5 — React 前端（`web/` 目录）

技术栈：React 18 + Vite + TS + TailwindCSS + Zustand + react-markdown

页面与组件：

| 组件 | 说明 |
|---|---|
| `LoginPage` | 注册/登录，存 JWT 到 localStorage |
| `ConversationList` | 左侧会话列表（侧栏） |
| `ChatWindow` | 右侧聊天窗口 |
| `MessageBubble` | 消息气泡，按 sender（用户/各 agent）区分头像与颜色——多 agent 人格在此体现 |
| `TaskCard` | 任务状态卡片：状态机实时展示（queued→running→succeeded/failed）、PR 链接、错误信息 |
| `ConfirmActions` | 任务预览的确认/取消按钮 |
| `ReportView` | markdown 报告渲染（`report_markdown`） |
| `useWebSocket` | hook：自动重连 + 指数退避、消息分发到 Zustand store |

> 📚 **学习点 ⑦**：断线重连与指数退避；WS 实时消息与 REST 历史消息的合并去重（按 message id）

### M6 — 收尾

- CORS 配置（开发期 Vite dev server 与 FastAPI 跨域）
- 生产部署：`vite build` 产物由 FastAPI `StaticFiles` 托管，单端口
- 测试（pytest + SQLite，沿用现有 `tests/` 模式）：
  - auth：注册/登录/错误密码/token 过期
  - 消息持久化：发消息 → 落库 → 拉历史
  - 回调 → 广播链路：模拟 worker 回调 → WS 客户端收到
- README 更新

---

## 5. 注释规范（贯穿所有新代码）

1. **每个模块头部**：docstring 写明职责 + 在整体架构中的位置（参考 `channels/feishu_channel.py` 的头部注释）
2. **公开类/函数**：Google 风格 docstring（参数、返回、异常）
3. **协议格式处必须注释**：WS 消息 schema、worker 回调 payload 的每个字段含义
4. **注释写 WHY 不写 WHAT**：为什么这样设计、踩过什么坑、什么替代方案被否决及原因
5. **面试学习点统一标记**：`# 📚 INTERVIEW:` 前缀，方便全局搜索复习
6. 全部中文注释

---

## 6. 面试学习点速查（落地后在代码中的位置）

| # | 知识点 | 位置 | 面试问题示例 |
|---|---|---|---|
| ① | 同步 IO 在 async 框架中的线程池处理 | `web_api/` 路由层 | "FastAPI 里能直接用 requests/psycopg 吗？会发生什么？" |
| ② | bcrypt 慢哈希、JWT 结构 | `web_api/auth.py` | "密码怎么存？JWT 怎么防伪？怎么主动失效？" |
| ③ | 依赖注入 | `web_api/deps.py` | "FastAPI 的 Depends 解决了什么问题？" |
| ④ | WebSocket 握手与升级 | `web_api/ws.py` | "WS 怎么建立的？和 HTTP 什么关系？" |
| ⑤ | 至少一次送达、幂等 | 回调路由 + `ws.py` | "推送丢了怎么办？重复回调怎么办？" |
| ⑥ | 状态机 + human-in-the-loop | `web_api/` 对话流 | "LLM 应用怎么做安全确认？" |
| ⑦ | 断线重连、指数退避 | `web/src/hooks/useWebSocket.ts` | "WS 断了怎么办？为什么指数退避？" |
| ⑧ | Webhook vs 轮询 vs 长连接 | 整体架构 | "三种消息获取方式的区别和选型？" |
| ⑨ | 状态汇总（子任务 → Job） | `agent_platform/bus.py`（既有） | "并行任务的状态怎么聚合？" |
| ⑩ | CORS、前后端分离部署 | M6 | "跨域是什么？预检请求什么时候发生？" |

---

## 7. 验证方式

- **M3 完成后**：启动服务 → `wscat -c ws://localhost:8081/ws/<conv>?token=...` → curl 模拟 worker 回调 → wscat 端实时收到状态消息
- **端到端**：前端发自然语言需求 → 确认 → local_worker 执行 → 回调 → 前端聊天窗口实时看到"运行中 → PR 已创建"
- 测试：`pytest tests/`（新增 auth、消息、广播用例）

---

## 8. 风险与备注

- **Python 版本**：`pyproject.toml` 要求 `>=3.11`，本机系统 Python 为 3.9.6，实施前需确认可用的 3.11+ 环境（venv/pyenv）
- **DB**：沿用 `DATABASE_URL`（默认 Postgres），本地开发/测试用 SQLite（`db.py` 的 JSON 类型已做方言兼容）
- **部署**：orchestrator 当前在腾讯云（111.231.5.52），新服务可与老服务同机不同端口双跑
- **不做的事**（第一版）：群聊/多人协作、文件传输、执行过程流式日志（worker 增量日志上报留待后续）
