from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import func, select  # noqa: E402

from agent_platform.db import (  # noqa: E402
    JobRow,
    TaskRow,
    WorkerJobRow,
    create_db_engine,
    create_session_factory,
    init_db,
    session_scope,
)
from agent_platform.store import PlatformStore  # noqa: E402
from agent_platform.worker_store import DurableWorkerQueue  # noqa: E402


def _load(path: Path, empty: Any) -> Any:
    if not path.exists():
        return empty
    return json.loads(path.read_text(encoding="utf-8"))


def _backup(path: Path) -> Path | None:
    """Create a non-destructive migration backup next to a legacy JSON file."""
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.migration-backup-{stamp}")
    if not target.exists():
        shutil.copy2(path, target)
    return target


def migrate(database_url: str, task_path: Path, queue_path: Path) -> dict[str, int]:
    engine = create_db_engine(database_url)
    init_db(engine)
    factory = create_session_factory(engine)
    store = PlatformStore(factory)
    durable_queue = DurableWorkerQueue(factory)
    counters = {"tasks": 0, "runs": 0, "worker_jobs": 0}

    raw_tasks = _load(task_path, {})
    task_items = raw_tasks.values() if isinstance(raw_tasks, dict) else raw_tasks
    for item in task_items:
        if not isinstance(item, dict):
            continue
        chat_id = str(item.get("chat_id") or "")
        if chat_id:
            task, _ = store.get_or_create_chat_task(
                tenant_key="default",
                chat_id=chat_id,
                goal=str(item.get("prompt") or ""),
                requester_id=str(item.get("requester_id") or ""),
                repo=str(item.get("repo") or ""),
                base_branch=str(item.get("base_branch") or ""),
                work_branch=str(item.get("work_branch") or ""),
            )
            task_id = task.id
        else:
            task_id = str(item.get("session_id") or item.get("id") or "")
        run_id = str(item.get("id") or "")
        if not task_id or not run_id:
            continue
        with session_scope(factory) as session:
            if session.get(JobRow, task_id) is None:
                session.add(
                    JobRow(
                        id=task_id,
                        tenant_key="default",
                        chat_id=chat_id,
                        requester_id=str(item.get("requester_id") or ""),
                        goal=str(item.get("prompt") or ""),
                        status="needs_attention"
                        if item.get("status") == "failed"
                        else str(item.get("status") or "received"),
                    )
                )
                session.flush()
            if session.get(TaskRow, run_id) is None:
                iteration = int(
                    session.scalar(
                        select(func.coalesce(func.max(TaskRow.iteration), 0)).where(
                            TaskRow.job_id == task_id
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    TaskRow(
                        id=run_id,
                        job_id=task_id,
                        agent_id="code",
                        goal=str(item.get("prompt") or ""),
                        inputs=item,
                        status=str(item.get("status") or "queued"),
                        phase=str(item.get("status") or "queued"),
                        iteration=iteration,
                        chat_id=chat_id,
                        requester_id=str(item.get("requester_id") or ""),
                    )
                )
                counters["runs"] += 1
        counters["tasks"] += 1

    raw_queue = _load(queue_path, [])
    for item in raw_queue if isinstance(raw_queue, list) else []:
        if not isinstance(item, dict):
            continue
        run_id = str(item.get("run_id") or item.get("job_id") or "")
        if not run_id:
            continue
        saved = durable_queue.enqueue(item)
        counters["worker_jobs"] += 1
        legacy_status = str(item.get("status") or "queued")
        if legacy_status == "claimed":
            with session_scope(factory) as session:
                row = session.scalar(select(WorkerJobRow).where(WorkerJobRow.run_id == run_id))
                if row is not None:
                    row.status = "needs_attention"
                    row.phase = "needs_attention"
                    row.error = "legacy claimed job has no verifiable lease owner"
                    row.terminal = True
                    run = session.get(TaskRow, row.run_id)
                    task = session.get(JobRow, row.task_id)
                    if run is not None:
                        run.status = "needs_attention"
                        run.phase = "needs_attention"
                    if task is not None:
                        task.status = "needs_attention"
        _ = saved
    return counters


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy JSON state into PostgreSQL")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--tasks", type=Path, default=ROOT / "data/tasks.json")
    parser.add_argument("--queue", type=Path, default=ROOT / "data/local_worker_queue.json")
    args = parser.parse_args()
    backups = [path for path in (_backup(args.tasks), _backup(args.queue)) if path]
    result = migrate(args.database_url, args.tasks, args.queue)
    print(json.dumps({"migrated": result, "backups": [str(path) for path in backups]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
