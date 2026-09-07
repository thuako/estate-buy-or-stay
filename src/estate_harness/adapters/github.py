"""GitHub Issues transport with durable keys and a fully offline equivalent.

The controller is the single writer. A write whose response was lost is reconciled
against the remote marker; it is never blindly retried. SSH credentials for Git do
not authenticate the REST API: this adapter uses the active ``gh`` API account.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROLES = ("orchestrator", "evidence", "policy", "market", "development", "decision", "auditor")
LABELS = {
    "harness": "5319e7",
    **{f"agent:{role}": "1d76db" for role in ROLES},
    **{f"kind:{kind}": "c5def5" for kind in ("run", "task", "discussion")},
    "state:queued": "ededed",
    "state:running": "fbca04",
    "state:done": "0e8a16",
    "state:blocked": "b60205",
}


class GitHubError(RuntimeError):
    """An actionable, credential-sanitized GitHub transport error."""


def marker(key: str) -> str:
    """Encode a stable controller key into an exact, hidden Markdown marker."""
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}", key):
        raise ValueError("GitHub idempotency key must be 1–240 safe identifier characters")
    return f"<!-- estate-harness:{key} -->"


def _body_with_marker(key: str, body: str) -> str:
    tag = marker(key)
    return body if tag in body else f"{body.rstrip()}\n\n{tag}"


def _sanitize(message: str) -> str:
    # Never include a known token even if gh (or a wrapper binary) echoed it.
    for name, value in os.environ.items():
        if (
            value
            and len(value) >= 8
            and any(part in name.upper() for part in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
        ):
            message = message.replace(value, "[REDACTED]")
    message = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[REDACTED]", message)
    message = re.sub(r"(?i)(authorization\s*:\s*(?:bearer|token)\s+)\S+", r"\1[REDACTED]", message)
    return " ".join(message.split())[:600]


def _transient(error: str) -> bool:
    return bool(
        re.search(
            r"(?i)(timed?\s*out|timeout|connection\s+(?:reset|refused)|network is unreachable|"
            r"temporary failure|no such host|unexpected eof|tls handshake|HTTP\s+50[234])",
            error,
        )
    )


def _labels(item: dict[str, Any]) -> list[str]:
    return [label if isinstance(label, str) else label["name"] for label in item.get("labels", [])]


def _merged_labels(current: dict[str, Any], desired: list[str]) -> list[str]:
    """Replace harness labels while preserving labels owned by people/tools."""
    return list(dict.fromkeys([name for name in _labels(current) if name not in LABELS] + desired))


def _find_marker(items: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    tag = marker(key)
    matches = [item for item in items if tag in (item.get("body") or "")]
    if len(matches) > 1:
        raise GitHubError(
            f"Multiple GitHub objects have the same harness key {key}; reconcile them before resuming"
        )
    return matches[0] if matches else None


def _number(number: int) -> int:
    if type(number) is not int or number < 1:
        raise ValueError("Issue number must be a positive integer")
    return number


class GitHubIssues:
    """Issue/comment API implemented with shell-free ``gh api`` calls."""

    def __init__(self, repo: str, *, gh_binary: str = "gh") -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("GitHub repository must be OWNER/REPO")
        self.repo = repo
        self.gh_binary = gh_binary
        self.base = f"repos/{repo}"

    def _api(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        argv = [
            self.gh_binary,
            "api",
            "--hostname",
            "github.com",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            endpoint,
        ]
        serialized = None
        if payload is not None:
            argv += ["--input", "-"]
            serialized = json.dumps(payload, ensure_ascii=False)
        # GET can be retried twice. No mutation is repeated inside this method.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                result = subprocess.run(
                    argv,
                    input=serialized,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except FileNotFoundError:
                raise GitHubError(
                    "GitHub CLI is missing; install gh and authenticate an account with repository Issues access"
                ) from None
            except subprocess.TimeoutExpired:
                detail = "GitHub API request timed out"
            except OSError as exc:
                detail = _sanitize(str(exc))
            else:
                if result.returncode == 0:
                    try:
                        return json.loads(result.stdout) if result.stdout.strip() else None
                    except (json.JSONDecodeError, TypeError):
                        raise GitHubError(
                            f"GitHub {method} returned invalid JSON; reconcile remote state before resuming"
                        ) from None
                detail = _sanitize(result.stderr or "GitHub CLI request failed")
            if attempt + 1 < attempts and _transient(detail):
                time.sleep(0.25 * (2**attempt))
                continue
            raise GitHubError(f"GitHub {method} {endpoint.split('?')[0]} failed: {detail}") from None
        raise AssertionError("unreachable")

    def _pages(self, endpoint: str) -> list[dict[str, Any]]:
        separator = "&" if "?" in endpoint else "?"
        output = []
        page = 1
        while True:
            chunk = self._api("GET", f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(chunk, list) or any(not isinstance(item, dict) for item in chunk):
                raise GitHubError("GitHub list endpoint returned an unexpected response")
            output.extend(chunk)
            if len(chunk) < 100:
                return output
            page += 1

    def check_access(self) -> dict[str, Any]:
        data = self._api("GET", self.base)
        if not isinstance(data, dict):
            raise GitHubError("GitHub repository endpoint returned an unexpected response")
        if not data.get("has_issues"):
            raise GitHubError(f"Issues are disabled for {self.repo}; enable them in repository settings")
        if data.get("archived"):
            raise GitHubError(f"Repository {self.repo} is archived and cannot accept harness updates")
        permissions = data.get("permissions") or {}
        if not any(permissions.get(level) for level in ("push", "maintain", "admin")):
            raise GitHubError(
                f"The active gh API account needs write access to {self.repo}; Git SSH authentication is separate"
            )
        return data

    def ensure_labels(self) -> None:
        existing = {item["name"] for item in self._pages(f"{self.base}/labels")}
        for name, color in LABELS.items():
            if name in existing:
                continue
            try:
                self._api(
                    "POST",
                    f"{self.base}/labels",
                    {
                        "name": name,
                        "color": color,
                        "description": "Estate research harness routing and state",
                    },
                )
            except GitHubError:
                # A parallel setup or a lost response may already have created it.
                if name not in {item["name"] for item in self._pages(f"{self.base}/labels")}:
                    raise
            existing.add(name)

    def list_issues(self) -> list[dict[str, Any]]:
        items = self._pages(f"{self.base}/issues?state=all&labels=harness&sort=created&direction=asc")
        # GitHub's Issues endpoint also returns pull requests.
        return [item for item in items if "pull_request" not in item]

    def get_issue(self, number: int) -> dict[str, Any]:
        item = self._api("GET", f"{self.base}/issues/{_number(number)}")
        if not isinstance(item, dict) or "pull_request" in item:
            raise GitHubError(f"#{number} is not a GitHub issue")
        return item

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self._pages(f"{self.base}/issues/{_number(number)}/comments")

    def ensure_issue(self, key: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        found = _find_marker(self.list_issues(), key)
        if found is not None:
            return found
        payload = {
            "title": title,
            "body": _body_with_marker(key, body),
            "labels": list(dict.fromkeys(["harness", *labels])),
        }
        try:
            result = self._api("POST", f"{self.base}/issues", payload)
        except GitHubError as write_error:
            try:
                found = _find_marker(self.list_issues(), key)
            except GitHubError:
                raise write_error from None
            if found is not None:
                return found
            raise write_error from None
        if not isinstance(result, dict) or "number" not in result:
            raise GitHubError(
                "Issue creation returned an unexpected response; resume to reconcile the marker"
            )
        return result

    def ensure_comment(self, number: int, key: str, body: str) -> dict[str, Any]:
        found = _find_marker(self.comments(number), key)
        if found is not None:
            return found
        try:
            result = self._api(
                "POST",
                f"{self.base}/issues/{_number(number)}/comments",
                {"body": _body_with_marker(key, body)},
            )
        except GitHubError as write_error:
            try:
                found = _find_marker(self.comments(number), key)
            except GitHubError:
                raise write_error from None
            if found is not None:
                return found
            raise write_error from None
        if not isinstance(result, dict) or "id" not in result:
            raise GitHubError(
                "Comment creation returned an unexpected response; resume to reconcile the marker"
            )
        return result

    def update_issue(
        self, number: int, *, state: str | None = None, labels: list[str] | None = None
    ) -> dict[str, Any]:
        _number(number)
        if state not in (None, "open", "closed"):
            raise ValueError("Issue state must be open or closed")
        payload: dict[str, Any] = {}
        if state is not None:
            payload["state"] = state
        if labels is not None:
            payload["labels"] = _merged_labels(self.get_issue(number), labels)
        if not payload:
            return self.get_issue(number)
        result = self._api("PATCH", f"{self.base}/issues/{number}", payload)
        if not isinstance(result, dict):
            raise GitHubError("Issue update returned an unexpected response")
        return result


class FixtureIssues:
    """Durable local issue board for offline tests; never contacts GitHub.

    Like the live adapter, this is for one controller. Writes atomically replace
    the JSON snapshot so interruption cannot leave half a JSON document.
    """

    def __init__(self, path: Path | str, repo: str = "fixture/estate-buy-or-stay") -> None:
        self.path = Path(path)
        self.repo = repo
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "repo": self.repo, "labels": [], "issues": [], "comments": {}}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise GitHubError(f"Cannot read fixture issue board at {self.path}") from None
        if state.get("schema_version") != 1 or state.get("repo") != self.repo:
            raise GitHubError("Fixture issue board schema or repository does not match")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _issue(state: dict[str, Any], number: int) -> dict[str, Any]:
        _number(number)
        for issue in state["issues"]:
            if issue["number"] == number and "pull_request" not in issue:
                return issue
        raise GitHubError(f"Fixture issue #{number} does not exist")

    def check_access(self) -> dict[str, Any]:
        return {
            "full_name": self.repo,
            "has_issues": True,
            "permissions": {"push": True, "admin": True},
            "html_url": f"https://github.com/{self.repo}",
            "fixture": True,
        }

    def ensure_labels(self) -> None:
        with self._lock:
            state = self._load()
            state["labels"] = list(dict.fromkeys([*state["labels"], *LABELS]))
            self._save(state)

    def list_issues(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(
                [
                    item
                    for item in self._load()["issues"]
                    if "pull_request" not in item and "harness" in _labels(item)
                ]
            )

    def get_issue(self, number: int) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._issue(self._load(), number))

    def comments(self, number: int) -> list[dict[str, Any]]:
        with self._lock:
            state = self._load()
            self._issue(state, number)
            return deepcopy(state["comments"].get(str(number), []))

    def ensure_issue(self, key: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            found = _find_marker(state["issues"], key)
            if found is not None:
                return deepcopy(found)
            number = max((item["number"] for item in state["issues"]), default=0) + 1
            now = datetime.now(timezone.utc).isoformat()
            item = {
                "number": number,
                "id": number,
                "title": title,
                "body": _body_with_marker(key, body),
                "labels": [{"name": name} for name in dict.fromkeys(["harness", *labels])],
                "state": "open",
                "html_url": f"https://github.com/{self.repo}/issues/{number}",
                "user": {"login": "estate-fixture"},
                "author_association": "OWNER",
                "created_at": now,
                "updated_at": now,
            }
            state["issues"].append(item)
            state["comments"][str(number)] = []
            self._save(state)
            return deepcopy(item)

    def ensure_comment(self, number: int, key: str, body: str) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            self._issue(state, number)
            comments = state["comments"].setdefault(str(number), [])
            found = _find_marker(comments, key)
            if found is not None:
                return deepcopy(found)
            comment_id = (
                max((comment["id"] for items in state["comments"].values() for comment in items), default=0)
                + 1
            )
            now = datetime.now(timezone.utc).isoformat()
            item = {
                "id": comment_id,
                "body": _body_with_marker(key, body),
                "html_url": f"https://github.com/{self.repo}/issues/{number}#issuecomment-{comment_id}",
                "user": {"login": "estate-fixture"},
                "author_association": "OWNER",
                "created_at": now,
                "updated_at": now,
            }
            comments.append(item)
            self._save(state)
            return deepcopy(item)

    def update_issue(
        self, number: int, *, state: str | None = None, labels: list[str] | None = None
    ) -> dict[str, Any]:
        if state not in (None, "open", "closed"):
            raise ValueError("Issue state must be open or closed")
        with self._lock:
            data = self._load()
            item = self._issue(data, number)
            if state is not None:
                item["state"] = state
            if labels is not None:
                item["labels"] = [{"name": name} for name in _merged_labels(item, labels)]
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save(data)
            return deepcopy(item)
