from __future__ import annotations

from .models import Task, TaskStatus


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

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**任务 ID**: `{task.id}`\n"
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
                "text": {"tag": "plain_text", "content": "继续修改"},
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
