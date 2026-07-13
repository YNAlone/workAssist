from __future__ import annotations

from typing import Protocol

from agent_platform.errors import DispatchError
from agent_platform.models import Job, JobStatus, PlatformTask, PlatformTaskStatus, utc_now
from agent_platform.registry import AgentRegistry
from agent_platform.store import PlatformStore


class Executor(Protocol):
    def can_handle(self, task: PlatformTask) -> bool: ...

    def dispatch(self, task: PlatformTask) -> None: ...

    def on_callback(self, payload: dict) -> PlatformTask: ...


class TaskBus:
    """Dispatches tasks to registered executors and rolls up job status."""

    def __init__(
        self,
        store: PlatformStore,
        registry: AgentRegistry,
        executors: dict[str, Executor],
    ) -> None:
        self.store = store
        self.registry = registry
        self.executors = executors

    def dispatch_task(self, task: PlatformTask) -> PlatformTask:
        agent = self.registry.get(task.agent_id)
        executor = self.executors.get(agent.id) or self.executors.get(agent.executor)
        if executor is None or not executor.can_handle(task):
            raise DispatchError(f"no executor for agent={task.agent_id} executor={agent.executor}")

        task.status = PlatformTaskStatus.DISPATCHED
        task.updated_at = utc_now()
        self.store.save_task(task)
        self.store.append_audit(
            event="task.dispatched",
            job_id=task.job_id,
            task_id=task.id,
            agent_id=task.agent_id,
            payload={"executor": agent.executor},
        )
        try:
            executor.dispatch(task)
        except Exception as exc:  # noqa: BLE001
            task.status = PlatformTaskStatus.FAILED
            task.error = str(exc)
            task.updated_at = utc_now()
            self.store.save_task(task)
            self.store.append_audit(
                event="task.dispatch_failed",
                job_id=task.job_id,
                task_id=task.id,
                agent_id=task.agent_id,
                payload={"error": str(exc)},
            )
            self.refresh_job_status(task.job_id)
            raise DispatchError(str(exc)) from exc
        return task

    def apply_callback(self, payload: dict) -> PlatformTask:
        task_id = str(payload.get("job_id") or payload.get("task_id") or "")
        if not task_id:
            raise DispatchError("callback missing job_id/task_id")

        task = self.store.require_task(task_id)
        agent = self.registry.get(task.agent_id)
        executor = self.executors.get(agent.id) or self.executors.get(agent.executor)
        if executor is None:
            raise DispatchError(f"no executor for callback agent={task.agent_id}")

        updated = executor.on_callback(payload)
        updated.updated_at = utc_now()
        self.store.save_task(updated)
        self.store.append_audit(
            event="task.callback",
            job_id=updated.job_id,
            task_id=updated.id,
            agent_id=updated.agent_id,
            payload={"status": updated.status.value, "error": updated.error},
        )
        self.refresh_job_status(updated.job_id)
        return updated

    def refresh_job_status(self, job_id: str) -> Job:
        job = self.store.require_job(job_id)
        tasks = self.store.list_tasks_for_job(job_id)
        if not tasks:
            return job

        statuses = {t.status for t in tasks}
        if statuses == {PlatformTaskStatus.SUCCEEDED}:
            job.status = JobStatus.DONE
        elif statuses == {PlatformTaskStatus.FAILED}:
            job.status = JobStatus.FAILED
        elif PlatformTaskStatus.FAILED in statuses and PlatformTaskStatus.SUCCEEDED in statuses:
            job.status = JobStatus.PARTIAL
        elif any(s in statuses for s in (PlatformTaskStatus.RUNNING, PlatformTaskStatus.DISPATCHED, PlatformTaskStatus.QUEUED)):
            job.status = JobStatus.RUNNING
        else:
            job.status = JobStatus.RUNNING

        job.task_ids = [t.id for t in tasks]
        job.updated_at = utc_now()
        self.store.save_job(job)
        return job
