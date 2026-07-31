from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

import certifi


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    def __init__(self, token: str, owner: str, repository: str, branch: str = "main"):
        self.token = token.strip()
        self.owner = owner.strip()
        self.repository = repository.strip()
        self.branch = branch.strip() or "main"
        self.api_root = "https://api.github.com"
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(3):
            request = urllib.request.Request(
                f"{self.api_root}{path}",
                data=body,
                method=method,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "GitDrop/0.1",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60, context=self.ssl_context) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    message = json.loads(exc.read().decode("utf-8")).get("message", str(exc))
                except (ValueError, UnicodeDecodeError):
                    message = str(exc)
                hints = {
                    401: "Token 无效或已过期",
                    403: "Token 没有仓库写入权限，或触发了 API 限制",
                    404: "仓库或分支不存在，或 Token 无权访问",
                    409: "仓库为空或当前提交状态冲突",
                    422: "GitHub 拒绝了本次请求",
                }
                raise GitHubError(
                    hints.get(exc.code, f"GitHub API 错误 {exc.code}") + f"：{message}",
                    exc.code,
                ) from exc
            except (
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                TimeoutError,
                urllib.error.URLError,
            ) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
        raise GitHubError(
            f"与 GitHub 的连接中断，请检查网络后重试：{last_error}"
        ) from last_error

    @property
    def repo_path(self) -> str:
        owner = urllib.parse.quote(self.owner, safe="")
        repository = urllib.parse.quote(self.repository, safe="")
        return f"/repos/{owner}/{repository}"

    def check_connection(self) -> str:
        data = self._request("GET", self.repo_path)
        return str(data.get("full_name", f"{self.owner}/{self.repository}"))

    def inspect_repository(self) -> dict[str, str] | None:
        """Read repository state without creating or changing anything."""
        if not self.token or not self.owner or not self.repository:
            raise GitHubError("请完整填写 GitHub 用户名、仓库名和 Token")
        user = self._request("GET", "/user")
        login = str(user.get("login", ""))
        if login.casefold() != self.owner.casefold():
            raise GitHubError(f"Token 属于 {login or '其他账号'}，与填写的用户名 {self.owner} 不一致")
        try:
            repository = self._request("GET", self.repo_path)
        except GitHubError as exc:
            if exc.status_code == 404:
                return None
            raise
        return {
            "full_name": str(repository.get("full_name") or f"{self.owner}/{self.repository}"),
            "url": str(repository.get("html_url") or f"https://github.com/{self.owner}/{self.repository}"),
            "branch": str(repository.get("default_branch") or "main"),
        }

    def ensure_repository(self, progress: Callable[[str], None] | None = None) -> bool:
        """Use an existing repository or create and initialize a private one."""
        progress = progress or (lambda _: None)
        progress("正在验证 GitHub 账号…")
        user = self._request("GET", "/user")
        login = str(user.get("login", ""))
        if login.casefold() != self.owner.casefold():
            raise GitHubError(f"Token 属于 {login or '其他账号'}，与填写的用户名 {self.owner} 不一致")

        progress("正在检查远端仓库…")
        created = False
        try:
            repository = self._request("GET", self.repo_path)
        except GitHubError as exc:
            if exc.status_code != 404:
                raise
            progress(f"未找到 {self.repository}，正在创建私有仓库…")
            repository = self._request(
                "POST",
                "/user/repos",
                {
                    "name": self.repository,
                    "description": "Messages and files synced by GitDrop",
                    "private": True,
                    "auto_init": True,
                },
            )
            created = True

        self.branch = str(repository.get("default_branch") or "main")
        return created
