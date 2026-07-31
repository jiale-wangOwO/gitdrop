from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import UploadItem


MAX_FILE_SIZE = 100 * 1024 * 1024
if getattr(sys, "frozen", False):
    executable = Path(sys.executable).resolve()
    APP_DIR = executable.parents[3] if platform.system() == "Darwin" else executable.parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = APP_DIR / ".gitdrop-cache"


class GitSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitSyncResult:
    commit_sha: str
    uploaded_files: int


class LocalGitTransport:
    def __init__(
        self,
        token: str,
        owner: str,
        repository: str,
        branch: str = "main",
        cache_dir: Path | None = None,
    ):
        self.token = token.strip()
        self.owner = owner.strip()
        self.repository = repository.strip()
        self.branch = branch.strip() or "main"
        self.cache_dir = cache_dir or CACHE_DIR

    @property
    def repository_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}"

    @property
    def clone_url(self) -> str:
        return f"https://{self.owner}@github.com/{self.owner}/{self.repository}.git"

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": str(self._askpass_path()),
                "GITDROP_TOKEN": self.token,
            }
        )
        return environment

    def _askpass_path(self) -> Path:
        return self.cache_dir / ("askpass.bat" if os.name == "nt" else "askpass.sh")

    def _prepare_cache(self) -> None:
        self.cleanup()
        self.cache_dir.mkdir(parents=True)
        askpass = self._askpass_path()
        if os.name == "nt":
            askpass.write_text("@echo off\necho %GITDROP_TOKEN%\n", encoding="ascii")
        else:
            askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$GITDROP_TOKEN"\n', encoding="ascii")
            askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    def cleanup(self) -> None:
        if not self.cache_dir.exists():
            return

        def remove_readonly(function, path, _error) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        for attempt in range(3):
            try:
                shutil.rmtree(self.cache_dir, onerror=remove_readonly)
                return
            except PermissionError:
                if attempt == 2:
                    raise GitSyncError(f"无法清理临时缓存：{self.cache_dir}")
                time.sleep(0.15)

    def _run(self, arguments: list[str], cwd: Path | None = None, timeout: int = 180) -> str:
        command = ["git", *arguments]
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=self._environment(),
                text=True,
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            system = "Git for Windows" if platform.system() == "Windows" else "Git"
            raise GitSyncError(f"未找到 Git，请先安装 {system}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitSyncError("连接 GitHub 超时，请检查网络后重试") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "Authentication failed" in detail or "could not read Password" in detail:
                detail = "Token 无效或没有仓库写入权限"
            raise GitSyncError(detail or f"Git 命令执行失败：{' '.join(arguments[:2])}")
        return result.stdout.strip()

    def _clone(self, progress: Callable[[str], None]) -> Path:
        progress("正在获取远端最新内容…")
        self._prepare_cache()
        worktree = self.cache_dir / "repo"
        self._run(
            ["clone", "--depth", "1", "--branch", self.branch, "--single-branch", self.clone_url, str(worktree)],
            timeout=300,
        )
        self._run(["config", "user.name", "GitDrop"], cwd=worktree)
        self._run(["config", "user.email", "gitdrop@localhost"], cwd=worktree)
        return worktree

    def sync(
        self,
        message: str,
        items: list[UploadItem],
        remote_folder: str,
        progress: Callable[[str], None] | None = None,
    ) -> GitSyncResult:
        progress = progress or (lambda _: None)
        for item in items:
            if item.size > MAX_FILE_SIZE:
                raise GitSyncError(f"文件超过 GitHub 100 MB 限制：{item.relative_path}")

        try:
            worktree = self._clone(progress)
            timestamp = datetime.now().astimezone()
            batch = worktree / (remote_folder.strip().strip("/\\") or "inbox") / timestamp.strftime("%Y-%m-%d_%H-%M-%S")
            batch.mkdir(parents=True, exist_ok=True)

            if message.strip():
                progress("正在整理消息…")
                note = (
                    f"# {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n\n{message.strip()}\n\n"
                    f"---\nSent with GitDrop at {timestamp.isoformat(timespec='seconds')}\n"
                )
                (batch / "message.md").write_text(note, encoding="utf-8")

            for index, item in enumerate(items, start=1):
                progress(f"正在整理 {index}/{len(items)}：{item.relative_path}")
                destination = batch.joinpath(*Path(item.relative_path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.source, destination)

            progress("正在创建本地提交…")
            self._run(["add", "--all"], cwd=worktree)
            summary = message.strip().splitlines()[0][:60] if message.strip() else f"Upload {len(items)} item(s)"
            self._run(["commit", "-m", f"GitDrop: {summary}"], cwd=worktree)
            commit_sha = self._run(["rev-parse", "HEAD"], cwd=worktree)
            progress("正在一次性推送到 GitHub…")
            self._run(["push", "origin", f"HEAD:{self.branch}"], cwd=worktree, timeout=600)
            return GitSyncResult(commit_sha, len(items) + int(bool(message.strip())))
        finally:
            self.cleanup()

    def clear_repository(self, progress: Callable[[str], None] | None = None) -> str:
        progress = progress or (lambda _: None)
        try:
            worktree = self._clone(progress)
            progress("正在清空仓库文件…")
            for child in worktree.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            self._run(["add", "--all"], cwd=worktree)
            status = self._run(["status", "--porcelain"], cwd=worktree)
            if not status:
                raise GitSyncError("仓库已经是空的")
            self._run(["commit", "-m", "GitDrop: clear repository contents"], cwd=worktree)
            commit_sha = self._run(["rev-parse", "HEAD"], cwd=worktree)
            progress("正在推送清理提交…")
            self._run(["push", "origin", f"HEAD:{self.branch}"], cwd=worktree, timeout=600)
            return commit_sha
        finally:
            self.cleanup()
