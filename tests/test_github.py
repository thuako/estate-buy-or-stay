"""Transport behavior without an account, network, or real GitHub mutations."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_harness.adapters.github import LABELS, FixtureIssues, GitHubError, GitHubIssues, marker


def response(data=None, *, error=""):
    return subprocess.CompletedProcess([], 1 if error else 0, json.dumps(data, ensure_ascii=False), error)


def issue(number=1, body="", **extra):
    return {
        "number": number,
        "body": body,
        "labels": [{"name": "harness"}],
        "state": "open",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        **extra,
    }


class GitHubTransportTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHubIssues("owner/repo")

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_issue_pagination_includes_closed_and_filters_pull_requests(self, run):
        first_page = [issue(n, state="closed") for n in range(1, 101)]
        first_page[0]["pull_request"] = {"url": "https://api.github.com/pulls/1"}
        run.side_effect = [response(first_page), response([issue(101)])]
        result = self.github.list_issues()
        self.assertEqual(len(result), 100)
        self.assertEqual(result[0]["number"], 2)
        endpoints = [
            call.args[0][call.args[0].index("X-GitHub-Api-Version: 2022-11-28") + 1]
            for call in run.call_args_list
        ]
        self.assertIn("state=all", endpoints[0])
        self.assertIn("labels=harness", endpoints[0])
        self.assertIn("per_page=100&page=2", endpoints[1])

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_comments_read_every_page_before_idempotency_check(self, run):
        existing = {"id": 101, "body": "existing\n" + marker("task:reply")}
        run.side_effect = [response([{"id": n, "body": "older"} for n in range(100)]), response([existing])]
        result = self.github.ensure_comment(5, "task:reply", "replacement must not be published")
        self.assertEqual(result["id"], 101)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all("POST" not in call.args[0] for call in run.call_args_list))

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_unicode_and_shell_characters_are_json_stdin(self, run):
        body = "@agent:market 전농·청량리 $(not-a-command) `literal`\n다음 질문"
        run.side_effect = [response([]), response(issue(7, body))]
        self.github.ensure_issue("run:test", "시장 논의", body, ["agent:market"])
        args, kwargs = run.call_args
        self.assertIn("--input", args[0])
        self.assertNotIn(body, args[0])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["encoding"], "utf-8")
        payload = json.loads(kwargs["input"])
        self.assertEqual(payload["title"], "시장 논의")
        self.assertIn(body, payload["body"])
        self.assertIn("전농", kwargs["input"])
        self.assertEqual(payload["body"].count(marker("run:test")), 1)
        self.assertEqual(payload["labels"], ["harness", "agent:market"])

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_lost_issue_write_response_reconciles_then_replay_finds_same_issue(self, run):
        created = issue(9, marker("run:lost"))
        run.side_effect = [
            response([]),
            response(error="connection reset by peer"),
            response([created]),
            response([created]),
        ]
        first = self.github.ensure_issue("run:lost", "title", "body", [])
        replayed = self.github.ensure_issue("run:lost", "title", "body", [])
        self.assertEqual(first["number"], 9)
        self.assertEqual(first, replayed)
        posts = [call for call in run.call_args_list if "POST" in call.args[0]]
        self.assertEqual(len(posts), 1)

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_lost_comment_write_response_reconciles_without_reposting(self, run):
        created = {"id": 23, "body": marker("task:review")}
        run.side_effect = [response([]), response(error="HTTP 502 Bad Gateway"), response([created])]
        result = self.github.ensure_comment(9, "task:review", "검토합니다")
        self.assertEqual(result, created)
        self.assertEqual(sum("POST" in call.args[0] for call in run.call_args_list), 1)

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_failed_issue_write_never_blindly_retries(self, run):
        run.side_effect = [response([]), response(error="connection reset by peer"), response([])]
        with self.assertRaises(GitHubError):
            self.github.ensure_issue("run:failed", "title", "body", [])
        self.assertEqual(sum("POST" in call.args[0] for call in run.call_args_list), 1)

    @patch("estate_harness.adapters.github.time.sleep")
    @patch("estate_harness.adapters.github.subprocess.run")
    def test_transient_reads_retry_twice_then_stop(self, run, sleep):
        run.return_value = response(error="connection reset by peer")
        with self.assertRaises(GitHubError):
            self.github.list_issues()
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_access_failure_does_not_retry_and_sanitizes_credentials(self, run):
        secret = "fake-secret-for-test-123456"
        with patch.dict(os.environ, {"GH_TOKEN": secret}):
            run.return_value = response(error=f"HTTP 403 denied {secret} ghp_visibleExample123")
            with self.assertRaises(GitHubError) as caught:
                self.github.check_access()
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("ghp_visibleExample123", str(caught.exception))
        self.assertIn("403", str(caught.exception))
        self.assertEqual(run.call_count, 1)

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_check_access_requires_issues_and_write_permission(self, run):
        for data in (
            {"has_issues": False, "permissions": {"admin": True}},
            {"has_issues": True, "permissions": {"pull": True}},
            {"has_issues": True, "archived": True, "permissions": {"push": True}},
        ):
            with self.subTest(data=data):
                run.return_value = response(data)
                with self.assertRaises(GitHubError):
                    self.github.check_access()
        valid = {"has_issues": True, "permissions": {"push": True}}
        run.return_value = response(valid)
        self.assertEqual(self.github.check_access(), valid)

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_update_preserves_labels_owned_by_people(self, run):
        original = issue(labels=[{"name": "harness"}, {"name": "state:queued"}, {"name": "user-priority"}])
        run.side_effect = [response(original), response(issue())]
        self.github.update_issue(1, state="closed", labels=["harness", "state:done"])
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload, {"state": "closed", "labels": ["user-priority", "harness", "state:done"]})

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_existing_labels_are_not_overwritten(self, run):
        run.return_value = response([{"name": name, "color": "123456"} for name in LABELS])
        self.github.ensure_labels()
        self.assertEqual(run.call_count, 1)

    @patch("estate_harness.adapters.github.subprocess.run")
    def test_duplicate_remote_markers_stop_ambiguous_resume(self, run):
        run.return_value = response([issue(1, marker("run:duplicate")), issue(2, marker("run:duplicate"))])
        with self.assertRaisesRegex(GitHubError, "Multiple GitHub objects"):
            self.github.ensure_issue("run:duplicate", "title", "body", [])
        self.assertEqual(run.call_count, 1)

    def test_invalid_repository_and_marker_are_rejected(self):
        with self.assertRaises(ValueError):
            GitHubIssues("owner/repo?redirect=somewhere")
        with self.assertRaises(ValueError):
            marker("key --> forged")


class FixtureIssuesTests(unittest.TestCase):
    def test_board_survives_restart_and_deduplicates_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "board.json"
            board = FixtureIssues(path)
            board.ensure_labels()
            first = board.ensure_issue(
                "run:fixture", "연구 시작", "@agent:evidence", ["agent:evidence", "user-label"]
            )
            posted = board.ensure_comment(first["number"], "reply:fixture", "@agent:market 검토 요청")
            reopened = FixtureIssues(path)
            self.assertEqual(reopened.ensure_issue("run:fixture", "ignored", "ignored", []), first)
            self.assertEqual(reopened.ensure_comment(first["number"], "reply:fixture", "ignored"), posted)
            result = reopened.update_issue(first["number"], state="closed", labels=["harness", "state:done"])
            self.assertEqual(result["state"], "closed")
            self.assertIn({"name": "user-label"}, result["labels"])
            self.assertEqual(len(reopened.list_issues()), 1)
            self.assertEqual(len(reopened.comments(first["number"])), 1)
            self.assertEqual(reopened.comments(first["number"])[0]["author_association"], "OWNER")
            self.assertEqual(reopened.comments(first["number"])[0]["user"]["login"], "estate-fixture")
            self.assertIn("연구 시작", path.read_text(encoding="utf-8"))
            self.assertFalse(list(path.parent.glob(".board.json.*")))

    def test_callers_cannot_mutate_fixture_state_by_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            board = FixtureIssues(Path(folder) / "board.json")
            first = board.ensure_issue("run:copy", "title", "body", [])
            first["body"] = "changed without a write"
            self.assertIn(marker("run:copy"), board.get_issue(1)["body"])

    def test_fixture_rejects_missing_issues_and_corrupt_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.json"
            board = FixtureIssues(path)
            with self.assertRaises(GitHubError):
                board.comments(99)
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(GitHubError):
                board.list_issues()


if __name__ == "__main__":
    unittest.main()
