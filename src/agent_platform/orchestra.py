from __future__ import annotations

from typing import Any

from .bus import TaskBus
from .errors import ValidationError
from .models import Job, JobStatus, PlatformTaskStatus, utc_now
from .planner import Planner
from .store import PlatformStore


class Orchestra:
    """Project-manager orchestrator: create jobs, plan, dispatch, summarize."""

    def __init__(self, store: PlatformStore, bus: TaskBus, planner: Planner | None = None) -> None:
        self.store = store
        self.bus = bus
        self.planner = planner or Planner()

    def create_and_dispatch(
        self,
        *,
        goal: str,
        requester_id: str = "",
        chat_id: str = "",
        agent_id: str = "",
        inputs: dict[str, Any] | None = None,
        auto_dispatch: bool = True,
        tenant_key: str = "default",
        command_key: str = "",
    ) -> dict[str, Any]:
        goal = (goal or "").strip()
        if not goal:
            raise ValidationError("goal is required")

        if chat_id:
            job, created = self.store.get_or_create_chat_task(
                tenant_key=tenant_key,
                chat_id=chat_id,
                goal=goal,
                requester_id=requester_id,
                repo=str((inputs or {}).get("repo") or ""),
                base_branch=str((inputs or {}).get("base_branch") or ""),
            )
            if created:
                self.store.append_audit(event="job.created", job_id=job.id, payload={"goal": goal})
        else:
            job = Job.create(
                goal=goal,
                requester_id=requester_id,
                chat_id=chat_id,
                tenant_key=tenant_key,
            )
            self.store.save_job(job)
            self.store.append_audit(event="job.created", job_id=job.id, payload={"goal": goal})
        job.status = JobStatus.PLANNED

        tasks = self.planner.plan_tasks(
            job_id=job.id,
            goal=goal,
            requester_id=requester_id,
            chat_id=chat_id,
            preferred_agent=agent_id,
            inputs=inputs,
        )
        job.plan = {
            "tasks": [{"agent_id": t.agent_id, "goal": t.goal, "inputs": t.inputs} for t in tasks]
        }
        dispatched: list[dict[str, Any]] = []
        next_iteration = len(self.store.list_tasks_for_job(job.id)) + 1
        for index, task in enumerate(tasks, start=1):
            task.job_id = job.id
            if command_key:
                task, _ = self.store.create_run_idempotent(
                    task_id=job.id,
                    command_key=f"{command_key}:{task.agent_id}",
                    agent_id=task.agent_id,
                    goal=task.goal,
                    inputs=task.inputs,
                    requester_id=requester_id,
                )
            else:
                task.iteration = next_iteration + index - 1
                self.store.save_task(task)
            if auto_dispatch:
                if task.status == PlatformTaskStatus.QUEUED:
                    task = self.bus.dispatch_task(task)
            dispatched.append(task.to_dict())

        job.task_ids = [t.id for t in self.store.list_tasks_for_job(job.id)]
        if dispatched:
            job.current_run_id = dispatched[-1]["id"]
        self.store.save_job(job)
        job = self.bus.refresh_job_status(job.id)
        if auto_dispatch and job.status == JobStatus.PLANNED:
            job.status = JobStatus.RUNNING
            job.updated_at = utc_now()
            self.store.save_job(job)

        return {
            "task_id": job.id,
            "run_id": dispatched[-1]["id"] if dispatched else "",
            "job": job.to_dict(),
            "tasks": dispatched,
        }

    def get_job_bundle(self, job_id: str) -> dict[str, Any]:
        job = self.store.require_job(job_id)
        tasks = self.store.list_tasks_for_job(job_id)
        return {"job": job.to_dict(), "tasks": [t.to_dict() for t in tasks]}
