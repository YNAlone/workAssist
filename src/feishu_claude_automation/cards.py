from __future__ import annotations

from .models import ConversationSession, Task, TaskStatus


EXECUTOR_LABELS = {
    "local_worker": "本机 Worker",
    "github_actions": "GitHub Actions",
    "gitlab_ci": "GitLab CI",
    "vcs": "VCS CI",
}

DELIVERY_LABELS = {
    "push": "推远程并开 PR/MR",
    "local_only": "仅本机工作区",
}


def _executor_label(value: str) -> str:
    return EXECUTOR_LABELS.get(value, value or "自动")


def _delivery_label(value: str) -> str:
    return DELIVERY_LABELS.get(value, value or "推远程并开 PR/MR")


def build_task_card(task: Task, *, interactive: bool = True) -> dict:
    status_label = {
        TaskStatus.RECEIVED: "已接收",
        TaskStatus.PENDING_APPROVAL: "待审批",
        TaskStatus.QUEUED: "排队中",
        TaskStatus.DISPATCHED: "已派发",
        TaskStatus.RUNNING: "执行中",
        TaskStatus.PR_CREATED: "已创建 PR",
        TaskStatus.FAILED: "失败",
        TaskStatus.CANCELLED: "已取消",
    }[task.status]

    mode_label = "迭代" if task.mode.value == "iterate" else "新建"
    executor_label = _executor_label(task.executor)
    delivery_label = "飞书文档（只读分析）" if task.analysis_only else _delivery_label(task.delivery)
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**任务 ID**: `{task.id}`\n"
                    f"**模式**: {mode_label}\n"
                    f"**执行方式**: {executor_label}\n"
                    f"**交付方式**: {delivery_label}\n"
                    f"**仓库**: `{task.repo}`\n"
                    f"**基线分支**: `{task.base_branch}`\n"
                    f"**工作分支**: `{task.work_branch or '无（只读分析）'}`\n"
                    f"**风险等级**: `{task.risk_level.value}`\n"
                    f"**状态**: {status_label}\n"
                    f"**需求**: {task.prompt}"
                ),
            },
        }
    ]

    if task.pr_url:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**PR**: [{task.pr_url}]({task.pr_url})"},
            }
        )
    if task.summary:
        # Feishu card field is limited; keep card short and put full report in chat reply.
        summary_preview = task.summary if len(task.summary) <= 500 else task.summary[:500] + "…"
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**摘要**: {summary_preview}"},
            }
        )
    if task.error:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**错误**: {task.error}"},
            }
        )
    if task.status == TaskStatus.PR_CREATED:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "直接回复本会话即可继续修改（将在同一分支上迭代）。",
                },
            }
        )

    actions: list[dict] = []
    if interactive and task.status == TaskStatus.PENDING_APPROVAL:
        actions.extend(
            [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "批准执行"},
                    "type": "primary",
                    "value": {"action": "approve", "task_id": task.id},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "type": "danger",
                    "value": {"action": "cancel", "task_id": task.id},
                },
            ]
        )
    elif interactive and task.status in {TaskStatus.PR_CREATED, TaskStatus.FAILED}:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "用原需求重跑"},
                "type": "default",
                "value": {"action": "rerun", "task_id": task.id},
            }
        )

    if actions:
        elements.append({"tag": "action", "actions": actions})
    elif not interactive and task.status == TaskStatus.PENDING_APPROVAL:
        # Legacy card actions require an HTTP callback. Text confirmation works over long connection.
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "请直接回复「确认执行」或「取消」。"},
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude 代码自动化任务"},
            "template": "blue" if task.status != TaskStatus.FAILED else "red",
        },
        "elements": elements,
    }


def build_confirm_plan_card(session: ConversationSession, *, interactive: bool = True) -> dict:
    executor_label = _executor_label(session.executor)
    delivery_label = "飞书文档（只读分析）" if session.analysis_only else _delivery_label(session.delivery)
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**会话 ID**: `{session.id}`\n"
                    f"**执行方式**: {executor_label}\n"
                    f"**交付方式**: {delivery_label}\n"
                    f"**仓库**: `{session.repo}`\n"
                    f"**基线分支**: `{session.base_branch}`\n"
                    f"**工作分支**: `{session.work_branch or ('无（只读分析）' if session.analysis_only else '(自动生成)')}`\n"
                    f"**需求**: {session.prompt}"
                ),
            },
        },
    ]
    if interactive:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认执行"},
                        "type": "primary",
                        "value": {"action": "confirm_execute", "session_id": session.id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消会话"},
                        "type": "danger",
                        "value": {"action": "cancel_session", "session_id": session.id},
                    },
                ],
            }
        )
    else:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "请直接回复「确认执行」或「取消」。"},
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "请确认执行计划"},
            "template": "turquoise",
        },
        "elements": elements,
    }
