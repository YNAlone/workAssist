from __future__ import annotations

from .models import ConversationSession, Task, TaskStatus


def build_task_card(task: Task) -> dict:
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
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**任务 ID**: `{task.id}`\n"
                    f"**模式**: {mode_label}\n"
                    f"**仓库**: `{task.repo}`\n"
                    f"**基线分支**: `{task.base_branch}`\n"
                    f"**工作分支**: `{task.work_branch}`\n"
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
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**摘要**: {task.summary}"},
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
    if task.status == TaskStatus.PENDING_APPROVAL:
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
    elif task.status in {TaskStatus.PR_CREATED, TaskStatus.FAILED}:
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

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude 代码自动化任务"},
            "template": "blue" if task.status != TaskStatus.FAILED else "red",
        },
        "elements": elements,
    }


def build_confirm_plan_card(session: ConversationSession) -> dict:
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**会话 ID**: `{session.id}`\n"
                    f"**仓库**: `{session.repo}`\n"
                    f"**基线分支**: `{session.base_branch}`\n"
                    f"**工作分支**: `{session.work_branch or '(自动生成)'}`\n"
                    f"**需求**: {session.prompt}"
                ),
            },
        },
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
        },
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "请确认执行计划"},
            "template": "turquoise",
        },
        "elements": elements,
    }
