"""A small GitHub REST client for this fork's maintenance scripts.

Standard library only, so the scripts run on a bare runner and on a
maintainer's machine without installing anything.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

API_VERSION = "2022-11-28"
PAGE_SIZE = 100
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GitHubError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, message: str) -> None:
        super().__init__(f"{method} {path} failed with HTTP {status}: {message}")
        self.status = status


class GitHub:
    """Calls `/repos/<repository>/...` endpoints with a bearer token."""

    def __init__(self, repository: str, token: str, api_url: str | None = None) -> None:
        if not REPOSITORY.fullmatch(repository):
            raise ValueError(f"expected OWNER/NAME, got {repository!r}")
        self.repository = repository
        self._token = token
        self._root = (
            api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com"
        ).rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        url = f"{self._root}/repos/{self.repository}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url, data=data, method=method, headers=self._headers(data)
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise GitHubError(error.code, method, path, _error_message(error)) from None
        return json.loads(payload) if payload else None

    def pages(
        self,
        path: str,
        query: dict[str, str | int] | None = None,
        key: str | None = None,
    ) -> Iterator[Any]:
        """Yields every item of a paginated list; `key` names the list inside an object reply."""
        page = 1
        while True:
            reply = self.request(
                "GET",
                path,
                query={**(query or {}), "per_page": PAGE_SIZE, "page": page},
            )
            items = reply[key] if key else reply
            yield from items
            if len(items) < PAGE_SIZE:
                return
            page += 1

    def _headers(self, data: bytes | None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "autogpt-desktop-fork-maintenance",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        return headers


def token_from_environment() -> str:
    """GH_TOKEN or GITHUB_TOKEN, else the token the `gh` CLI is signed in with."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if value := os.environ.get(name, "").strip():
            return value
    if value := _token_from_gh():
        return value
    raise SystemExit(
        "No GitHub token: set GH_TOKEN or GITHUB_TOKEN, or sign in with `gh auth login`."
    )


def _token_from_gh() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _error_message(error: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(error.read()).get("message", error.reason))
    except (ValueError, AttributeError):
        return str(error.reason)
