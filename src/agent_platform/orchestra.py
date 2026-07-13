from __future__ import annotations

from typing import Any

from .bus import TaskBus
from .errors import ValidationError
from .models import Job, JobStatus, utc_now
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
    ) -> dict[str, Any]:
        goal = (goal or "").strip()
        if not goal:
            raise ValidationError("goal is required")

        job = Job.create(goal=goal, requester_id=requester_id, chat_id=chat_id)
        job.status = JobStatus.PLANNED
        self.store.save_job(job)
        self.store.append_audit(event="job.created", job_id=job.id, payload={"goal": goal})

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
        job.task_ids = [t.id for t in tasks]
        self.store.save_job(job)

        dispatched: list[dict[str, Any]] = []
        for task in tasks:
            self.store.save_task(task)
            if auto_dispatch:
                task = self.bus.dispatch_task(task)
            dispatched.append(task.to_dict())

        job = self.bus.refresh_job_status(job.id)
        if auto_dispatch and job.status == JobStatus.PLANNED:
            job.status = JobStatus.RUNNING
            job.updated_at = utc_now()
            self.store.save_job(job)

        return {
            "job": job.to_dict(),
            "tasks": dispatched,
        }

    def get_job_bundle(self, job_id: str) -> dict[str, Any]:
        job = self.store.require_job(job_id)
        tasks = self.store.list_tasks_for_job(job_id)
        return {"job": job.to_dict(), "tasks": [t.to_dict() for t in tasks]}
