from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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


def _normalise_proxy_url(proxy: str | None) -> str | None:
    if not proxy:
        return None

    proxy = proxy.strip()
    if not proxy:
        return None

    if "://" not in proxy:
        proxy = f"http://{proxy}"

    return proxy


def _detect_proxy() -> str | None:
    """Return the proxy GitDrop should pass to Git."""
    environment_names = (
        "GITDROP_HTTPS_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    )

    for name in environment_names:
        proxy = _normalise_proxy_url(os.environ.get(name))
        if proxy:
            return proxy

    if platform.system() != "Darwin":
        return None

    try:
        if urllib.request.proxy_bypass("github.com"):
            return None
        proxies = urllib.request.getproxies()
    except (OSError, ValueError):
        return None

    return _normalise_proxy_url(
        proxies.get("https") or proxies.get("http") or proxies.get("all")
    )


def _proxy_display_name(proxy: str) -> str:
    """Return a diagnostic-safe proxy URL without credentials or path data."""
    try:
        parsed = urllib.parse.urlsplit(proxy)
        if not parsed.hostname:
            return "已配置代理"
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return "已配置代理"


def _proxy_has_credentials(proxy: str) -> bool:
    """Return whether a proxy URL contains user information."""
    try:
        parsed = urllib.parse.urlsplit(proxy)
        return parsed.username is not None or parsed.password is not None
    except ValueError:
        return "@" in proxy


def _redact_proxy_credentials(detail: str, proxy: str | None) -> str:
    """Remove proxy credentials and their encoded forms from Git output."""
    if not proxy or not _proxy_has_credentials(proxy):
        return detail

    try:
        parsed = urllib.parse.urlsplit(proxy)
    except ValueError:
        parsed = None

    sensitive_values = {proxy, urllib.parse.unquote(proxy)}
    if parsed is not None:
        if "@" in parsed.netloc:
            user_info = parsed.netloc.rsplit("@", 1)[0]
            sensitive_values.update((user_info, urllib.parse.unquote(user_info)))
        for credential in (parsed.username, parsed.password):
            if credential:
                sensitive_values.update(
                    (credential, urllib.parse.unquote(credential))
                )

    for value in tuple(sensitive_values):
        sensitive_values.update(
            (
                urllib.parse.quote(value, safe=""),
                urllib.parse.quote_plus(value, safe=""),
            )
        )

    for value in sorted(filter(None, sensitive_values), key=len, reverse=True):
        detail = detail.replace(value, "[REDACTED_PROXY_CREDENTIALS]")

    return detail


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
        proxy_url: str | None = None,
    ):
        self.token = token.strip()
        self.owner = owner.strip()
        self.repository = repository.strip()
        self.branch = branch.strip() or "main"
        self.cache_dir = cache_dir or CACHE_DIR
        self.proxy_url = (
            _normalise_proxy_url(proxy_url)
            if proxy_url is not None
            else _detect_proxy()
        )

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
        if self.proxy_url:
            environment.update(
                {
                    "HTTPS_PROXY": self.proxy_url,
                    "https_proxy": self.proxy_url,
                    "HTTP_PROXY": self.proxy_url,
                    "http_proxy": self.proxy_url,
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
        command = ["git"]
        if self.proxy_url and not _proxy_has_credentials(self.proxy_url):
            command.extend(["-c", f"http.proxy={self.proxy_url}"])
        command.extend(arguments)
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
            detail = _redact_proxy_credentials(detail, self.proxy_url)
            if self.token:
                detail = detail.replace(self.token, "[REDACTED]")
            if "Authentication failed" in detail or "could not read Password" in detail:
                detail = "Token 无效或没有仓库写入权限"
            network_markers = (
                "SSL_ERROR_SYSCALL",
                "Connection reset by peer",
                "Recv failure",
                "Could not resolve host",
                "Failed to connect",
                "Connection timed out",
            )
            if any(marker.lower() in detail.lower() for marker in network_markers):
                proxy_hint = (
                    f"当前检测到代理：{_proxy_display_name(self.proxy_url)}"
                    if self.proxy_url
                    else "未检测到可供 Git 使用的代理"
                )
                detail = (
                    f"{detail}\n\n{proxy_hint}。\n"
                    "请检查系统代理、VPN/代理节点，以及 github.com 是否命中代理规则。"
                )
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
