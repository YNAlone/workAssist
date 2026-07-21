from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import Settings
from .github import GitHubClient
from .local_worker_client import LocalWorkerClient
from .policy import Policy


class LocalWorkerRunner:
    """Execute queued jobs on the local machine using Claude Code CLI."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.policy = Policy.load(settings.policy_file)
        self.client = LocalWorkerClient(settings, self.policy)

    def run_forever(self) -> None:
        poll = max(1, self.settings.local_worker_poll_seconds)
        if self.client.remote_mode:
            source = f"remote={self.settings.local_worker_orchestrator_url}"
        else:
            source = f"queue={self.settings.local_worker_queue_path}"
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
                self.execute_job(job)
                self.client.complete(str(job.get("job_id") or ""), status="completed")
            except Exception as exc:  # noqa: BLE001
                try:
                    self._callback(
                        job,
                        {
                            "status": "failed",
                            "error": str(exc),
                        },
                    )
                except Exception as callback_exc:  # noqa: BLE001
                    print(f"callback failed: {callback_exc}")
                try:
                    self.client.complete(str(job.get("job_id") or ""), status="failed")
                except Exception as complete_exc:  # noqa: BLE001
                    print(f"complete failed: {complete_exc}")
            time.sleep(0.2)

    def execute_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        local_path = Path(str(job.get("local_path") or ""))
        if not local_path.is_dir():
            raise RuntimeError(f"local_path does not exist: {local_path}")

        self._callback(job, {"status": "running"})

        mode = str(job.get("mode") or "create")
        base_branch = str(job.get("base_branch") or "")
        work_branch = str(job.get("work_branch") or "")
        prompt = str(job.get("prompt") or "")
        delivery = str(job.get("delivery") or "push")

        self._git_checkout(local_path, mode=mode, base_branch=base_branch, work_branch=work_branch)
        self._run_claude(local_path, prompt)
        report_path, report_text = self._locate_report(local_path, job_id)

        if delivery == "local_only":
            diff_stat = self._git_diff_stat(local_path)
            summary = "本机工作区已更新（未推远程）"
            if report_text:
                summary = "分析报告已生成本地文件（未推远程）"
            self._callback(
                job,
                {
                    "status": "succeeded",
                    "summary": summary,
                    "delivery": "local_only",
                    "worktree_path": str(local_path),
                    "diff_stat": diff_stat,
                    "report_path": report_path,
                    "report_markdown": report_text[:12000] if report_text else "",
                },
            )
            return

        commit_sha, no_changes = self._commit_and_push(
            local_path,
            job_id=job_id,
            work_branch=work_branch,
            provider=str(job.get("provider") or "github"),
            repo=str(job.get("repo") or ""),
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

        if report_text and (no_changes or not pr_url):
            status = "completed"
            summary = "分析报告已生成"
        elif no_changes and not report_text:
            status = "completed"
            summary = "任务完成但未生成分析文档或代码改动。"
        elif mode == "iterate":
            status = "updated"
            summary = "分支已更新"
        else:
            status = "pr_created"
            summary = "PR/MR 已创建" if pr_url else "变更已推送"

        self._callback(
            job,
            {
                "status": status,
                "pr_url": pr_url,
                "summary": summary,
                "commit_sha": commit_sha,
                "delivery": "push",
                "report_path": report_path,
                "report_markdown": report_text[:12000] if report_text else "",
            },
        )

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
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"command failed ({' '.join(args)}): {detail}")
        return (result.stdout or "").strip()

    def _git_checkout(
        self,
        repo_path: Path,
        *,
        mode: str,
        base_branch: str,
        work_branch: str,
    ) -> None:
        self._run(["git", "fetch", "origin", base_branch, work_branch], cwd=repo_path)
        if mode == "iterate":
            self._run(["git", "checkout", "-B", work_branch, f"origin/{work_branch}"], cwd=repo_path)
        else:
            self._run(["git", "checkout", "-B", work_branch, f"origin/{base_branch}"], cwd=repo_path)

    def _run_claude(self, repo_path: Path, prompt: str) -> None:
        if self.settings.dry_run:
            return
        if not self.settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for local worker")

        env = {
            "ANTHROPIC_API_KEY": self.settings.anthropic_api_key,
            "ANTHROPIC_BASE_URL": self.settings.anthropic_base_url,
            "ANTHROPIC_MODEL": self.settings.anthropic_model,
        }
        self._run(
            [
                "claude",
                "-p",
                "--dangerously-skip-permissions",
                "--max-turns",
                "50",
                "--model",
                self.settings.anthropic_model,
                "--allowedTools",
                "Edit,Read,Write,Bash",
                prompt,
            ],
            cwd=repo_path,
            env=env,
        )

    def _locate_report(self, repo_path: Path, job_id: str) -> tuple[str, str]:
        target = repo_path / f"docs/analysis-{job_id}.md"
        if target.is_file():
            return str(target.relative_to(repo_path)), target.read_text(encoding="utf-8", errors="replace")

        candidates = sorted(repo_path.glob("docs/analysis-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            chosen = candidates[0]
            return str(chosen.relative_to(repo_path)), chosen.read_text(encoding="utf-8", errors="replace")
        return "", ""

    def _git_diff_stat(self, repo_path: Path) -> str:
        try:
            return self._run(["git", "diff", "--stat"], cwd=repo_path)
        except RuntimeError:
            return self._run(["git", "status", "--short"], cwd=repo_path)

    def _commit_and_push(
        self,
        repo_path: Path,
        *,
        job_id: str,
        work_branch: str,
        provider: str,
        repo: str,
    ) -> tuple[str, bool]:
        if self._is_clean(repo_path):
            return "", True

        self._run(["git", "add", "-A"], cwd=repo_path)
        self._run(["git", "commit", "-m", f"feat: {job_id} automated change"], cwd=repo_path)
        commit_sha = self._run(["git", "rev-parse", "HEAD"], cwd=repo_path)

        if self.settings.dry_run:
            return commit_sha, False

        remote_url = self._authenticated_remote(provider=provider, repo=repo, repo_path=repo_path)
        self._run(["git", "push", remote_url, f"HEAD:refs/heads/{work_branch}"], cwd=repo_path)
        return commit_sha, False

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

        job_id = str(job.get("job_id") or "")
        if provider == "gitlab":
            return self._create_gitlab_mr(
                repo=repo,
                base_branch=base_branch,
                work_branch=work_branch,
                job_id=job_id,
                prompt=prompt,
                mode=mode,
            )
        return self._create_github_pr(
            repo=repo,
            base_branch=base_branch,
            work_branch=work_branch,
            job_id=job_id,
            prompt=prompt,
            mode=mode,
        )

    def _create_github_pr(
        self,
        *,
        repo: str,
        base_branch: str,
        work_branch: str,
        job_id: str,
        prompt: str,
        mode: str,
    ) -> str:
        env = {
            **os.environ,
            "GH_TOKEN": self.settings.github_token,
            "GITHUB_TOKEN": self.settings.github_token,
        }
        if mode == "iterate":
            result = subprocess.run(
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
            if result.returncode == 0 and (result.stdout or "").strip():
                return result.stdout.strip()

        result = subprocess.run(
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
                f"AI: {job_id}",
                "--body",
                f"Automated change from Feishu task {job_id}.\n\nPrompt:\n{prompt}",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"gh pr create failed: {detail}")
        return (result.stdout or "").strip()

    def _create_gitlab_mr(
        self,
        *,
        repo: str,
        base_branch: str,
        work_branch: str,
        job_id: str,
        prompt: str,
        mode: str,
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

        if mode == "iterate":
            return ""

        payload = {
            "source_branch": work_branch,
            "target_branch": base_branch,
            "title": f"AI: {job_id}",
            "description": f"Automated change from Feishu task {job_id}.\n\nPrompt:\n{prompt}",
            "remove_source_branch": False,
        }
        created = self._http_post(f"{base}/projects/{project}/merge_requests", payload, headers=headers)
        return str(created.get("web_url") or "")

    def _resolve_callback_url(self, job: dict[str, Any]) -> str:
        callback_url = str(job.get("callback_url") or "").strip()
        if not callback_url or not self.client.remote_mode:
            return callback_url
        orch = self.settings.local_worker_orchestrator_url.rstrip("/")
        if not orch:
            return callback_url
        lowered = callback_url.lower()
        if "localhost" in lowered or "127.0.0.1" in lowered:
            return f"{orch}/callbacks/runner"
        return callback_url

    def _callback(self, job: dict[str, Any], payload: dict[str, Any]) -> None:
        callback_url = self._resolve_callback_url(job)
        if not callback_url:
            return
        body = {**payload, "job_id": job.get("job_id")}
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
