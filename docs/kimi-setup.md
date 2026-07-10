# Kimi Code API 配置说明

本项目通过 GitHub Actions 调用 Claude Code，并将 API 指向 Kimi Code 端点。

## GitHub Secrets 配置

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名称 | 值 |
|-------------|-----|
| `ANTHROPIC_API_KEY` | 你的 Kimi Code API Key（`sk-kimi-...`） |

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
2. 在 Orchestrator 手动创建任务，或从飞书发送 `/ai-fix` 指令
3. 打开 GitHub Actions，查看 `Feishu Claude Automation` 是否成功运行

## 安全提醒

- 不要把 API Key 提交到 Git 仓库
- 如果 Key 已在聊天或截图中泄露，请立即在 Kimi Code 控制台重置
