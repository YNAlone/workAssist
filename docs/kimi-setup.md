# Kimi Code API 配置说明

本项目在两处使用 Kimi：

1. **GitHub Actions Runner**：通过 Claude Code Action 改代码（仓库 Secret `ANTHROPIC_API_KEY`）
2. **Orchestrator**：自然语言意图理解与多轮对话（环境变量 `ORCH_LLM_API_KEY`）

## Orchestrator 侧

在 `.env` 中配置：

| 变量 | 说明 |
|------|------|
| `ORCH_LLM_API_KEY` | Kimi API Key（`sk-kimi-...`） |
| `ORCH_LLM_BASE_URL` | 默认 `https://api.kimi.com/coding/` |
| `ORCH_LLM_MODEL` | 意图解析模型，可选 `kimi-for-coding`（默认）或 `k3`（别名 `K3`） |
| `ANTHROPIC_MODEL` | CI / 本机 Worker 执行模型，可选同上；调度时会传给 workflow |

`AUTOMATION_DRY_RUN=true` 时不会调用 LLM，使用内置 mock 意图解析。

## 可选模型

| 配置值 | API Model ID | 说明 |
|--------|--------------|------|
| `kimi-for-coding` | `kimi-for-coding` | Kimi K2.7 Code，默认，全员可用 |
| `k3` / `K3` | `k3` | Kimi K3，需 Moderato 及以上套餐 |
| `kimi-for-coding-highspeed` | `kimi-for-coding-highspeed` | K2.7 高速版，需 Allegretto 及以上 |

示例：两处都切到 K3：

```bash
ORCH_LLM_MODEL=k3
ANTHROPIC_MODEL=k3
```

也可只改其中一处（例如意图解析用 `kimi-for-coding`，改代码用 `k3`）。

## 会话中临时选择执行模型

自然语言可指定本轮/本会话的执行模型（写入会话并随任务下发；未指定则用 `ANTHROPIC_MODEL`）：

- 「用 k3」「切换到 k3」「模型 kimi-for-coding」
- 命令：`/ai-fix repo=... branch=... model=k3 desc="..."`

确认卡片与任务卡片会显示当前执行模型。

## GitHub Secrets 配置

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名称 | 值 |
|-------------|-----|
| `ANTHROPIC_API_KEY` | 你的 Kimi Code API Key（`sk-kimi-...`） |
| `GH_PAT`（推荐） | 具有 `repo` 权限的 PAT，用于创建 PR |

API 地址已在 workflow 中配置，无需额外 Secret：

```text
https://api.kimi.com/coding/
```

模型由 Orchestrator 的 `ANTHROPIC_MODEL` 通过 workflow input `model` 传入（默认 `kimi-for-coding`）。

## 命令行添加 Secret（可选）

```bash
gh secret set ANTHROPIC_API_KEY --repo YNAlone/workAssist
```

按提示粘贴 API Key。

## 验证

1. 推送 workflow 到 GitHub
2. 在 Orchestrator `.env` 填好 `ORCH_LLM_API_KEY` 并启动服务
3. 在飞书对机器人说自然语言，例如：「帮我在 workAssist 项目新增 xxx 功能」
4. 确认计划卡片后，打开 GitHub Actions 查看 `Feishu Claude Automation`

也可继续使用结构化命令：

```text
/ai-fix repo=YNAlone/workAssist branch=main desc="Fix refund rounding bug"
```

## 安全提醒

- 不要把 API Key 提交到 Git 仓库
- 如果 Key 已在聊天或截图中泄露，请立即在 Kimi Code 控制台重置
