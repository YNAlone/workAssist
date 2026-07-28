from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .local_worker_client import LocalWorkerClient
from .policy import Policy


class LeaseLostError(RuntimeError):
    """Raised before side effects when the worker can no longer renew its lease."""


class NeedsAttentionError(RuntimeError):
    """Raised when automatic recovery could overwrite an ambiguous code state."""


class _LeaseHeartbeat:
    """Renew a worker lease in the background and fence the attached process."""

    def __init__(self, runner: LocalWorkerRunner, job: dict[str, Any]) -> None:
        self.runner = runner
        self.job = job
        self.phase = "leased"
        self.failures = 0
        self.lost = threading.Event()
        self.stop_event = threading.Event()
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> _LeaseHeartbeat:
        if self.job.get("lease_token"):
            try:
                self.runner.client.heartbeat(self.job, phase=self.phase)
            except Exception as exc:  # noqa: BLE001
                self.lost.set()
                raise LeaseLostError(f"unable to establish worker heartbeat: {exc}") from exc
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def attach(self, process: subprocess.Popen[str]) -> None:
        self.process = process

    def detach(self) -> None:
        self.process = None

    def ensure_owned(self) -> None:
        if self.lost.is_set():
            raise LeaseLostError("worker lease lost; side effects have been fenced")

    def _run(self) -> None:
        interval = max(1, self.runner.settings.local_worker_heartbeat_seconds)
        while not self.stop_event.wait(interval):
            try:
                self.runner.client.heartbeat(self.job, phase=self.phase)
                self.failures = 0
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                print(f"heartbeat failed ({self.failures}/3): {exc}")
                if self.failures < 3:
                    continue
                self.lost.set()
                process = self.process
                if process is not None and process.poll() is None:
                    process.terminate()
                return


class LocalWorkerRunner:
    """Execute lease-backed jobs in persistent Git worktrees with Claude Code CLI."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.policy = Policy.load(settings.policy_file)
        self.client = LocalWorkerClient(settings, self.policy)

    def run_forever(self) -> None:
        poll = max(1, self.settings.local_worker_poll_seconds)
        source = (
            f"remote={self.settings.local_worker_orchestrator_url}"
            if self.client.remote_mode
            else "database queue"
        )
        print(f"local worker polling every {poll}s ({source})")
        while True:
            try:
                job = self.client.claim()
            except Exception as exc:  # noqa: BLE001
                print(f"claim failed: {exc}")
                time.sleep(poll)
                continue
            if job is None:
                time.sleep(poll)
                continue
            try:
                result = self.execute_job(job)
                self.client.complete(job, status="completed", result=result)
            except Exception as exc:  # noqa: BLE001
                status = self._failure_status(job, exc)
                self._callback(job, {"status": status, "error": str(exc)})
                try:
                    self.client.complete(
                        job,
                        status=status,
                        result={
                            "claude_session_id": str(job.get("claude_session_id") or ""),
                            "worktree_path": str(job.get("worktree_path") or ""),
                        },
                        error=str(exc),
                    )
                except Exception as complete_exc:  # noqa: BLE001
                    print(f"complete failed: {complete_exc}")
            time.sleep(0.2)

    def _failure_status(self, job: dict[str, Any], exc: Exception) -> str:
        """Retry once only when the preserved worktree can be resumed safely."""
        if isinstance(exc, (NeedsAttentionError, LeaseLostError)):
            return "needs_attention"
        if int(job.get("recovery_count") or 0) >= self.settings.local_worker_max_recoveries:
            return "needs_attention"
        raw_path = str(job.get("worktree_path") or "")
        worktree = Path(raw_path) if raw_path else None
        has_session = bool(job.get("claude_session_id"))
        clean = worktree is None or not worktree.exists() or self._is_clean(worktree)
        return "awaiting_retry" if has_session or clean else "needs_attention"

    def execute_job(self, job: dict[str, Any]) -> dict[str, Any]:
        run_id = str(job.get("run_id") or job.get("job_id") or "")
        task_id = str(job.get("task_id") or run_id)
        source_repo = Path(str(job.get("local_path") or ""))
        if not source_repo.is_dir():
            raise RuntimeError(f"local_path does not exist: {source_repo}")

        mode = str(job.get("mode") or "create")
        base_branch = str(job.get("base_branch") or "")
        work_branch = str(job.get("work_branch") or "")
        prompt = str(job.get("prompt") or "")
        delivery = str(job.get("delivery") or "push")
        model = str(job.get("model") or self.settings.anthropic_model)
        prior_result = job.get("result") if isinstance(job.get("result"), dict) else {}
        claude_session_id = str(
            job.get("claude_session_id") or prior_result.get("claude_session_id") or ""
        )

        with _LeaseHeartbeat(self, job) as heartbeat:
            self._transition(job, heartbeat, "preparing_worktree")
            worktree = self._prepare_worktree(
                source_repo,
                task_id=task_id,
                mode=mode,
                base_branch=base_branch,
                work_branch=work_branch,
            )
            job["worktree_path"] = str(worktree)
            if job.get("recovery") and not claude_session_id and not self._is_clean(worktree):
                raise NeedsAttentionError(
                    "expired run has uncommitted changes but no Claude session; worktree was preserved"
                )

            self._transition(
                job,
                heartbeat,
                "running_claude",
                payload={"worktree_path": str(worktree)},
            )
            claude_session_id = self._run_claude(
                worktree,
                prompt,
                model=model,
                session_id=claude_session_id,
                job=job,
                heartbeat=heartbeat,
            )
            verification = self._verify_with_repairs(
                worktree,
                job=job,
                heartbeat=heartbeat,
                model=model,
                session_id=claude_session_id,
            )
            report_path, report_text = self._locate_report(worktree, run_id)

            if delivery == "local_only":
                result = {
                    "status": "succeeded",
                    "summary": "本机工作区已更新（未推送远程）",
                    "delivery": "local_only",
                    "worktree_path": str(worktree),
                    "diff_stat": self._git_diff_stat(worktree),
                    "report_path": report_path,
                    "report_markdown": report_text[:12000] if report_text else "",
                    "claude_session_id": claude_session_id,
                    "verification": verification,
                }
                self._callback(job, result)
                return result

            heartbeat.ensure_owned()
            self._transition(
                job,
                heartbeat,
                "committing",
                payload={
                    "claude_session_id": claude_session_id,
                    "verification": verification,
                },
            )
            commit_sha, no_changes = self._commit_changes(
                worktree,
                run_id=run_id,
                existing_sha=str(prior_result.get("commit_sha") or ""),
            )
            self._transition(job, heartbeat, "pushing", payload={"commit_sha": commit_sha})
            remote_sha = self._push_idempotently(
                worktree,
                commit_sha=commit_sha,
                work_branch=work_branch,
                provider=str(job.get("provider") or "github"),
                repo=str(job.get("repo") or ""),
            )
            heartbeat.ensure_owned()
            self._transition(
                job,
                heartbeat,
                "opening_mr",
                payload={"commit_sha": commit_sha, "remote_sha": remote_sha},
            )
            pr_url = ""
            if not no_changes:
                pr_url = self._create_change_request(
                    job,
                    provider=str(job.get("provider") or "github"),
                    repo=str(job.get("repo") or ""),
                    base_branch=base_branch,
                    work_branch=work_branch,
                    prompt=prompt,
                    mode=mode,
                )

            status = "completed" if no_changes else ("updated" if mode == "iterate" else "pr_created")
            summary = (
                "任务完成，没有产生新的代码变更。"
                if no_changes
                else ("分支已更新。" if mode == "iterate" else "PR/MR 已创建。" if pr_url else "变更已推送。")
            )
            result = {
                "status": status,
                "pr_url": pr_url,
                "summary": summary,
                "commit_sha": commit_sha,
                "remote_sha": remote_sha,
                "delivery": "push",
                "worktree_path": str(worktree),
                "report_path": report_path,
                "report_markdown": report_text[:12000] if report_text else "",
                "claude_session_id": claude_session_id,
                "verification": verification,
            }
            self._transition(job, heartbeat, "succeeded", payload=result)
            self._callback(job, result)
            return result

    def _transition(
        self,
        job: dict[str, Any],
        heartbeat: _LeaseHeartbeat,
        phase: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist a phase event before continuing to the next operation."""
        heartbeat.set_phase(phase)
        heartbeat.ensure_owned()
        sequence = int(job.get("_event_sequence") or 0) + 1
        job["_event_sequence"] = sequence
        if job.get("lease_token"):
            self.client.event(
                job,
                sequence=sequence,
                event_type="phase",
                phase=phase,
                payload=payload or {},
            )
        self._callback(job, {"status": "running", "phase": phase, **(payload or {})})

    def _run(self, args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
        merged = {**os.environ, **(env or {})}
        result = subprocess.run(
            args,
            cwd=cwd,
            env=merged,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = self._redact_command([(result.stderr or result.stdout or "").strip()])
            rendered = self._redact_command(args)
            raise RuntimeError(f"command failed ({rendered}): {detail}")
        return (result.stdout or "").strip()

    @staticmethod
    def _redact_command(args: list[str]) -> str:
        """Prevent embedded Git credentials from leaking into errors and callbacks."""
        rendered = " ".join(args)
        return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", rendered)

    def _prepare_worktree(
        self,
        source_repo: Path,
        *,
        task_id: str,
        mode: str,
        base_branch: str,
        work_branch: str,
    ) -> Path:
        """Create or reuse the task's stable worktree without touching the source checkout."""
        if not base_branch or not work_branch:
            raise RuntimeError("base_branch and work_branch are required")
        repo_slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", source_repo.name).strip("-") or "repo"
        configured_root = self.settings.local_worker_worktree_root
        root = configured_root if configured_root.is_absolute() else source_repo.parent / configured_root
        worktree = (root / repo_slug / task_id).resolve()
        self._run(["git", "rev-parse", "--git-dir"], cwd=source_repo)
        self._run(["git", "fetch", "origin", base_branch], cwd=source_repo)

        if worktree.exists():
            if not worktree.is_dir():
                raise NeedsAttentionError(f"worktree path is not a directory: {worktree}")
            actual = self._run(["git", "branch", "--show-current"], cwd=worktree)
            if actual != work_branch:
                raise NeedsAttentionError(
                    f"worktree branch mismatch: expected {work_branch}, found {actual or 'detached'}"
                )
            return worktree

        worktree.parent.mkdir(parents=True, exist_ok=True)
        if self._local_branch_exists(source_repo, work_branch):
            self._run(["git", "worktree", "add", str(worktree), work_branch], cwd=source_repo)
        elif self._remote_branch_exists(source_repo, work_branch):
            self._run(
                ["git", "worktree", "add", "-b", work_branch, str(worktree), f"origin/{work_branch}"],
                cwd=source_repo,
            )
        else:
            self._run(
                ["git", "worktree", "add", "-b", work_branch, str(worktree), f"origin/{base_branch}"],
                cwd=source_repo,
            )
        return worktree

    @staticmethod
    def _local_branch_exists(repo_path: Path, branch: str) -> bool:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _remote_branch_exists(repo_path: Path, branch: str) -> bool:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _run_claude(
        self,
        repo_path: Path,
        prompt: str,
        *,
        model: str = "",
        session_id: str = "",
        job: dict[str, Any] | None = None,
        heartbeat: _LeaseHeartbeat | None = None,
    ) -> str:
        """Run Claude as a stream and persist normalized events plus raw JSONL."""
        if self.settings.dry_run:
            return session_id
        if not self.settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for local worker")

        resolved_model = (model or self.settings.anthropic_model).strip() or self.settings.anthropic_model
        args = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            "50",
            "--model",
            resolved_model,
            "--allowedTools",
            "Edit,Read,Write,Bash",
        ]
        if session_id:
            args.extend(["--resume", session_id])
        args.append(prompt)
        env = {
            **os.environ,
            "ANTHROPIC_API_KEY": self.settings.anthropic_api_key,
            "ANTHROPIC_BASE_URL": self.settings.anthropic_base_url,
            "ANTHROPIC_MODEL": resolved_model,
        }
        raw_log = self._raw_log_path(job or {})
        process = subprocess.Popen(
            args,
            cwd=repo_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if heartbeat is not None:
            heartbeat.attach(process)
        resolved_session = session_id
        try:
            with raw_log.open("a", encoding="utf-8") as handle:
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line = raw_line.rstrip("\r\n")
                    handle.write(line + "\n")
                    handle.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {"type": "console", "text": line}
                    resolved_session = self._session_from_event(event) or resolved_session
                    if resolved_session and job is not None:
                        job["claude_session_id"] = resolved_session
                    if job and job.get("lease_token"):
                        sequence = int(job.get("_event_sequence") or 0) + 1
                        job["_event_sequence"] = sequence
                        self.client.event(
                            job,
                            sequence=sequence,
                            event_type=str(event.get("type") or "claude"),
                            phase="running_claude",
                            payload=self._normalize_claude_event(event, resolved_session),
                        )
            return_code = process.wait()
        finally:
            if heartbeat is not None:
                heartbeat.detach()
        if heartbeat is not None:
            heartbeat.ensure_owned()
        if return_code != 0:
            raise RuntimeError(f"Claude Code exited with status {return_code}; raw log: {raw_log}")
        return resolved_session

    def _raw_log_path(self, job: dict[str, Any]) -> Path:
        task_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(job.get("task_id") or "unknown"))
        run_id = re.sub(
            r"[^a-zA-Z0-9._-]+",
            "-",
            str(job.get("run_id") or job.get("job_id") or "unknown"),
        )
        attempt = int(job.get("attempt_no") or 0)
        path = self.settings.local_worker_log_root / task_id / run_id / f"attempt-{attempt}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._prune_raw_logs()
        return path

    def _prune_raw_logs(self) -> None:
        """Apply bounded retention without traversing outside the configured log root."""
        root = self.settings.local_worker_log_root.resolve()
        if not root.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=max(1, self.settings.local_worker_log_retention_days)
        )
        for path in root.rglob("*.jsonl"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff and root in path.resolve().parents:
                path.unlink(missing_ok=True)

    @staticmethod
    def _session_from_event(event: dict[str, Any]) -> str:
        value = event.get("session_id")
        if value:
            return str(value)
        result = event.get("result")
        if isinstance(result, dict) and result.get("session_id"):
            return str(result["session_id"])
        return ""

    @staticmethod
    def _normalize_claude_event(event: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Keep useful structured fields while bounding database event size."""
        normalized = {
            "type": str(event.get("type") or "claude"),
            "subtype": str(event.get("subtype") or ""),
            "session_id": session_id,
            "claude_session_id": session_id,
        }
        for key in ("message", "result", "tool", "tool_name", "is_error", "duration_ms"):
            value = event.get(key)
            if value is not None:
                encoded = json.dumps(value, ensure_ascii=False)
                normalized[key] = value if len(encoded) <= 12000 else encoded[:12000] + "…"
        return normalized

    def _verify_with_repairs(
        self,
        repo_path: Path,
        *,
        job: dict[str, Any],
        heartbeat: _LeaseHeartbeat,
        model: str,
        session_id: str,
    ) -> dict[str, Any]:
        commands = [
            str(command)
            for command in (job.get("verify_commands") or [])
            if str(command).strip()
        ]
        if not commands:
            return {"status": "skipped", "commands": []}

        last_results: list[dict[str, Any]] = []
        for repair_no in range(self.settings.local_worker_max_repair_loops + 1):
            phase = "verifying" if repair_no == 0 else "repairing"
            self._transition(
                job,
                heartbeat,
                phase,
                payload={"claude_session_id": session_id},
            )
            last_results = self._run_verification_commands(repo_path, commands)
            if all(item["returncode"] == 0 for item in last_results):
                return {"status": "passed", "commands": last_results, "repair_loops": repair_no}
            if repair_no >= self.settings.local_worker_max_repair_loops:
                break
            failures = "\n\n".join(
                f"$ {item['command']}\n{item['output']}" for item in last_results if item["returncode"]
            )
            repair_prompt = (
                "Verification failed. Fix only the code required to make these repository checks pass, "
                "then stop. Do not commit or push.\n\n" + failures[:16000]
            )
            session_id = self._run_claude(
                repo_path,
                repair_prompt,
                model=model,
                session_id=session_id,
                job=job,
                heartbeat=heartbeat,
            )
        raise NeedsAttentionError(
            "verification still fails after repair loops: "
            + json.dumps(last_results, ensure_ascii=False)[:4000]
        )

    @staticmethod
    def _run_verification_commands(
        repo_path: Path,
        commands: list[str],
    ) -> list[dict[str, Any]]:
        """Execute policy-owned commands; shell use is explicit configuration, not model output."""
        results: list[dict[str, Any]] = []
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=repo_path,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            output = ((completed.stdout or "") + (completed.stderr or "")).strip()
            results.append(
                {
                    "command": command,
                    "returncode": completed.returncode,
                    "output": output[-12000:],
                }
            )
        return results

    def _locate_report(self, repo_path: Path, run_id: str) -> tuple[str, str]:
        target = repo_path / f"docs/analysis-{run_id}.md"
        if target.is_file():
            return str(target.relative_to(repo_path)), target.read_text(
                encoding="utf-8", errors="replace"
            )
        candidates = sorted(
            repo_path.glob("docs/analysis-*.md"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            chosen = candidates[0]
            return str(chosen.relative_to(repo_path)), chosen.read_text(
                encoding="utf-8", errors="replace"
            )
        return "", ""

    def _git_diff_stat(self, repo_path: Path) -> str:
        try:
            return self._run(["git", "diff", "--stat"], cwd=repo_path)
        except RuntimeError:
            return self._run(["git", "status", "--short"], cwd=repo_path)

    def _commit_changes(
        self,
        repo_path: Path,
        *,
        run_id: str,
        existing_sha: str = "",
    ) -> tuple[str, bool]:
        head = self._run(["git", "rev-parse", "HEAD"], cwd=repo_path)
        if existing_sha and head == existing_sha and self._is_clean(repo_path):
            return existing_sha, False
        if self._is_clean(repo_path):
            subject = self._run(["git", "log", "-1", "--pretty=%s"], cwd=repo_path)
            if run_id in subject:
                # Recovery may happen after git commit but before the SHA event was persisted.
                return head, False
            return head, True
        if self.settings.dry_run:
            return head, False
        self._run(["git", "add", "-A"], cwd=repo_path)
        self._run(["git", "commit", "-m", f"feat: {run_id} automated change"], cwd=repo_path)
        return self._run(["git", "rev-parse", "HEAD"], cwd=repo_path), False

    def _push_idempotently(
        self,
        repo_path: Path,
        *,
        commit_sha: str,
        work_branch: str,
        provider: str,
        repo: str,
    ) -> str:
        if self.settings.dry_run:
            return commit_sha
        remote_url = self._authenticated_remote(provider=provider, repo=repo, repo_path=repo_path)
        remote_sha = self._remote_branch_sha(repo_path, remote_url, work_branch)
        if remote_sha == commit_sha:
            return remote_sha
        self._run(["git", "push", remote_url, f"HEAD:refs/heads/{work_branch}"], cwd=repo_path)
        return self._remote_branch_sha(repo_path, remote_url, work_branch) or commit_sha

    @staticmethod
    def _remote_branch_sha(repo_path: Path, remote_url: str, branch: str) -> str:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url, f"refs/heads/{branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return ""
        return result.stdout.split()[0]

    def _is_clean(self, repo_path: Path) -> bool:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return status.returncode == 0 and not (status.stdout or "").strip()

    def _authenticated_remote(self, *, provider: str, repo: str, repo_path: Path) -> str:
        origin = self._run(["git", "remote", "get-url", "origin"], cwd=repo_path)
        if provider == "gitlab":
            token = self.settings.gitlab_token
            if not token:
                raise RuntimeError("GITLAB_TOKEN is required to push from local worker")
            if origin.startswith("http"):
                parsed = urllib.parse.urlparse(origin)
                host = parsed.netloc.split("@")[-1]
                path = parsed.path.lstrip("/")
                return f"https://oauth2:{token}@{host}/{path}"
            return origin
        token = self.settings.github_token
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required to push from local worker")
        return f"https://x-access-token:{token}@github.com/{repo}.git"

    def _create_change_request(
        self,
        job: dict[str, Any],
        *,
        provider: str,
        repo: str,
        base_branch: str,
        work_branch: str,
        prompt: str,
        mode: str,
    ) -> str:
        if self.settings.dry_run:
            return ""
        run_id = str(job.get("run_id") or job.get("job_id") or "")
        if provider == "gitlab":
            return self._create_gitlab_mr(
                repo=repo,
                base_branch=base_branch,
                work_branch=work_branch,
                run_id=run_id,
                prompt=prompt,
            )
        return self._create_github_pr(
            repo=repo,
            base_branch=base_branch,
            work_branch=work_branch,
            run_id=run_id,
            prompt=prompt,
        )

    def _create_github_pr(
        self,
        *,
        repo: str,
        base_branch: str,
        work_branch: str,
        run_id: str,
        prompt: str,
    ) -> str:
        env = {
            **os.environ,
            "GH_TOKEN": self.settings.github_token,
            "GITHUB_TOKEN": self.settings.github_token,
        }
        existing = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                work_branch,
                "--base",
                base_branch,
                "--state",
                "open",
                "--json",
                "url",
                "-q",
                ".[0].url",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if existing.returncode == 0 and (existing.stdout or "").strip():
            return existing.stdout.strip()
        created = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                base_branch,
                "--head",
                work_branch,
                "--title",
                f"AI: {run_id}",
                "--body",
                f"Automated change from Feishu run {run_id}.\n\nPrompt:\n{prompt}",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if created.returncode != 0:
            detail = (created.stderr or created.stdout or "").strip()
            raise RuntimeError(f"gh pr create failed: {detail}")
        return (created.stdout or "").strip()

    def _create_gitlab_mr(
        self,
        *,
        repo: str,
        base_branch: str,
        work_branch: str,
        run_id: str,
        prompt: str,
    ) -> str:
        token = self.settings.gitlab_token
        if not token:
            raise RuntimeError("GITLAB_TOKEN is required to create GitLab MR")
        project = urllib.parse.quote(repo, safe="")
        base = self.settings.gitlab_api_base.rstrip("/")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "PRIVATE-TOKEN": token,
        }
        list_url = (
            f"{base}/projects/{project}/merge_requests"
            f"?source_branch={urllib.parse.quote(work_branch)}"
            f"&target_branch={urllib.parse.quote(base_branch)}"
            f"&state=opened"
        )
        existing = self._http_get(list_url, headers=headers)
        if isinstance(existing, list) and existing:
            return str(existing[0].get("web_url") or "")
        payload = {
            "source_branch": work_branch,
            "target_branch": base_branch,
            "title": f"AI: {run_id}",
            "description": f"Automated change from Feishu run {run_id}.\n\nPrompt:\n{prompt}",
            "remove_source_branch": False,
        }
        created = self._http_post(
            f"{base}/projects/{project}/merge_requests",
            payload,
            headers=headers,
        )
        return str(created.get("web_url") or "")

    def _callback(self, job: dict[str, Any], payload: dict[str, Any]) -> None:
        callback_url = str(job.get("callback_url") or "")
        if not callback_url:
            return
        run_id = job.get("run_id") or job.get("job_id")
        body = {
            **payload,
            "task_id": job.get("task_id"),
            "run_id": run_id,
            "job_id": run_id,
            "attempt_no": job.get("attempt_no", 0),
            "lease_token": job.get("lease_token", ""),
        }
        if self.settings.dry_run:
            print(f"[dry-run] callback -> {callback_url}: {json.dumps(body, ensure_ascii=False)}")
            return
        self._http_post(callback_url, body, headers={"Content-Type": "application/json"})

    @staticmethod
    def _http_get(url: str, *, headers: dict[str, str]) -> Any:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP GET {url} failed {exc.code}: {detail}") from exc

    @staticmethod
    def _http_post(url: str, payload: dict[str, Any], *, headers: dict[str, str]) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP POST {url} failed {exc.code}: {detail}") from exc


def main() -> None:
    settings = Settings.from_env()
    if not settings.local_worker_enabled and not settings.dry_run:
        raise SystemExit("LOCAL_WORKER_ENABLED is false")
    LocalWorkerRunner(settings).run_forever()


if __name__ == "__main__":
    main()
