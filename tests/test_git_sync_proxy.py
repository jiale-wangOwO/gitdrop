from __future__ import annotations

import os
import subprocess
import unittest
import urllib.parse
from unittest.mock import patch

from gitdrop.git_sync import (
    GitSyncError,
    LocalGitTransport,
    _detect_proxy,
    _normalise_proxy_url,
)


class ProxyDetectionTests(unittest.TestCase):
    def test_normalises_proxy_without_scheme(self):
        self.assertEqual(
            _normalise_proxy_url(" 127.0.0.1:1082 "),
            "http://127.0.0.1:1082",
        )

    def test_explicit_gitdrop_proxy_has_highest_priority(self):
        environment = {
            "GITDROP_HTTPS_PROXY": "http://127.0.0.1:1082",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(_detect_proxy(), "http://127.0.0.1:1082")

    def test_standard_proxy_precedence(self):
        environment = {
            "ALL_PROXY": "socks5h://127.0.0.1:7891",
            "HTTP_PROXY": "http://127.0.0.1:7890",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(_detect_proxy(), "socks5h://127.0.0.1:7891")

    @patch("gitdrop.git_sync.platform.system", return_value="Darwin")
    @patch("gitdrop.git_sync.urllib.request.proxy_bypass", return_value=False)
    @patch(
        "gitdrop.git_sync.urllib.request.getproxies",
        return_value={"https": "http://127.0.0.1:1082"},
    )
    def test_macos_system_proxy_is_detected(
        self,
        _getproxies,
        _proxy_bypass,
        _system,
    ):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_detect_proxy(), "http://127.0.0.1:1082")

    @patch("gitdrop.git_sync.platform.system", return_value="Darwin")
    @patch("gitdrop.git_sync.urllib.request.proxy_bypass", return_value=False)
    @patch(
        "gitdrop.git_sync.urllib.request.getproxies",
        return_value={"http": "127.0.0.1:1082"},
    )
    def test_macos_http_proxy_is_fallback(
        self,
        _getproxies,
        _proxy_bypass,
        _system,
    ):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_detect_proxy(), "http://127.0.0.1:1082")

    @patch("gitdrop.git_sync.platform.system", return_value="Darwin")
    @patch("gitdrop.git_sync.urllib.request.proxy_bypass", return_value=True)
    def test_proxy_bypass_is_respected(self, _proxy_bypass, _system):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_detect_proxy())

    @patch("gitdrop.git_sync.platform.system", return_value="Linux")
    @patch("gitdrop.git_sync.urllib.request.getproxies")
    def test_non_macos_does_not_read_system_proxy(self, getproxies, _system):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_detect_proxy())
        getproxies.assert_not_called()


class GitProxyPropagationTests(unittest.TestCase):
    def test_no_proxy_preserves_original_command_shape(self):
        transport = LocalGitTransport(
            "secret-token",
            "owner",
            "repository",
            proxy_url="",
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gitdrop.git_sync.subprocess.run", return_value=completed) as run,
        ):
            self.assertEqual(transport._run(["status"]), "ok")

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(command, ["git", "status"])
        self.assertNotIn("secret-token", command)
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertEqual(environment["GITDROP_TOKEN"], "secret-token")

    def test_proxy_is_passed_to_command_and_environment(self):
        proxy = "http://127.0.0.1:1082"
        transport = LocalGitTransport(
            "secret-token",
            "owner",
            "repository",
            proxy_url=proxy,
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

        with patch("gitdrop.git_sync.subprocess.run", return_value=completed) as run:
            transport._run(["clone", "remote", "local"])

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            command,
            ["git", "-c", f"http.proxy={proxy}", "clone", "remote", "local"],
        )
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            self.assertEqual(environment[name], proxy)
        self.assertNotIn("secret-token", command)

    def test_credentialed_proxy_is_only_passed_in_environment(self):
        proxy = "http://proxy-user:proxy-password@127.0.0.1:1082"
        transport = LocalGitTransport(
            "secret-token",
            "owner",
            "repository",
            proxy_url=proxy,
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

        with patch("gitdrop.git_sync.subprocess.run", return_value=completed) as run:
            transport._run(["clone", "remote", "local"])

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(command, ["git", "clone", "remote", "local"])
        self.assertNotIn(proxy, command)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            self.assertEqual(environment[name], proxy)

    def test_network_error_has_proxy_hint_without_credentials(self):
        proxy = "http://proxy-user:proxy-password@127.0.0.1:1082/private"
        transport = LocalGitTransport(
            "secret-token",
            "owner",
            "repository",
            proxy_url=proxy,
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"Recv failure through {proxy}: Connection reset by peer",
        )

        with patch("gitdrop.git_sync.subprocess.run", return_value=completed):
            with self.assertRaises(GitSyncError) as raised:
                transport._run(["push"])

        detail = str(raised.exception)
        self.assertIn("当前检测到代理：http://127.0.0.1:1082", detail)
        self.assertNotIn(proxy, detail)
        self.assertNotIn("proxy-user", detail)
        self.assertNotIn("proxy-password", detail)
        self.assertNotIn("secret-token", detail)

    def test_url_encoded_proxy_credentials_are_redacted_from_git_errors(self):
        proxy = "http://proxy-user:p%40ssword@127.0.0.1:1082"
        encoded_proxy = urllib.parse.quote(proxy, safe="")
        transport = LocalGitTransport(
            "secret-token",
            "owner",
            "repository",
            proxy_url=proxy,
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"Failed to connect using {encoded_proxy}",
        )

        with patch("gitdrop.git_sync.subprocess.run", return_value=completed):
            with self.assertRaises(GitSyncError) as raised:
                transport._run(["push"])

        detail = str(raised.exception)
        self.assertIn("[REDACTED_PROXY_CREDENTIALS]", detail)
        self.assertNotIn(encoded_proxy, detail)
        self.assertNotIn("proxy-user", detail)
        self.assertNotIn("p%40ssword", detail)
        self.assertNotIn("p@ssword", detail)

    def test_token_is_redacted_from_git_errors(self):
        transport = LocalGitTransport(
            "secret-token",
            "owner",
            "repository",
            proxy_url="",
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="unexpected output containing secret-token",
        )

        with patch("gitdrop.git_sync.subprocess.run", return_value=completed):
            with self.assertRaises(GitSyncError) as raised:
                transport._run(["status"])

        detail = str(raised.exception)
        self.assertIn("[REDACTED]", detail)
        self.assertNotIn("secret-token", detail)


if __name__ == "__main__":
    unittest.main()
