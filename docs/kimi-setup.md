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
| `ORCH_LLM_MODEL` | 默认 `kimi-for-coding` |

`AUTOMATION_DRY_RUN=true` 时不会调用 LLM，使用内置 mock 意图解析。

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

模型 ID：

```text
kimi-for-coding
```

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
