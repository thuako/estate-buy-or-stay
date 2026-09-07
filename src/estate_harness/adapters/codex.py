from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from estate_harness.schemas import AgentResult
from estate_harness.store import atomic_json


class ProviderError(RuntimeError):
    pass


class ToolBudget:
    def __init__(self, remaining: int):
        self.remaining = remaining
        self.used = 0
        self.lock = threading.Lock()

    def consume(self) -> bool:
        with self.lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            self.used += 1
            return True


def strict_schema(schema: dict) -> dict:
    """Codex structured output requires required keys even for nullable fields."""
    schema = json.loads(json.dumps(schema))

    def visit(value):
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema


class CodexProvider:
    """Isolated, read-only CLI workers; account usage, not a USD-priced API adapter."""

    name = "codex"

    def __init__(self, model: str | None = None):
        self.model = model

    def check(self) -> str:
        process = subprocess.run(
            ["codex", "login", "status"], capture_output=True, text=True, timeout=30, check=False
        )
        status = process.stdout + process.stderr
        if process.returncode:
            raise ProviderError("Codex authentication unavailable; run codex login")
        if "api key" in status.lower() or os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY"):
            raise ProviderError(
                "This adapter supports Codex account usage only. API billing needs a "
                "separate adapter with explicit USD prices and reservation limits."
            )
        return status.strip()

    def run(self, task: dict, prompt: str, output_dir: Path, timeout: float, tools: ToolBudget) -> dict:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="estate-agent-") as temporary:
            work = Path(temporary)
            schema_file, result_file = work / "schema.json", work / "result.json"
            atomic_json(schema_file, strict_schema(AgentResult.model_json_schema()))
            command = [
                "codex",
                "--search",
                "-a",
                "never",
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-C",
                str(work),
                "--json",
                "--output-schema",
                str(schema_file),
                "--output-last-message",
                str(result_file),
            ]
            # Workers can research through web search; controller owns execution and writes.
            for feature in (
                "shell_tool",
                "apps",
                "plugins",
                "multi_agent",
                "hooks",
                "computer_use",
                "browser_use",
                "in_app_browser",
            ):
                command.extend(["--disable", feature])
            if self.model:
                command.extend(["--model", self.model])
            command.append("-")
            env = {
                key: value
                for key, value in os.environ.items()
                if not any(term in key.upper() for term in ("TOKEN", "API_KEY", "GITHUB", "GH_"))
            }
            started = time.monotonic()
            usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "cost_usd": None,
                "billing": "codex_account",
                "model": self.model,
            }
            failure: list[str] = []
            with (
                (output_dir / "events.jsonl").open("w") as events,
                (output_dir / "stderr.log").open("w") as log,
            ):
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    text=True,
                    env=env,
                    start_new_session=True,
                )

                def stop(reason: str):
                    failure.append(reason)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

                timer = threading.Timer(max(0.1, timeout), stop, args=("Worker timeout",))
                timer.start()
                try:
                    process.stdin.write(prompt)
                    process.stdin.close()
                    seen_tools = set()
                    for line in process.stdout:
                        events.write(line)
                        events.flush()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        item = event.get("item", {})
                        if item.get("type") in ("web_search", "command_execution", "mcp_tool_call"):
                            identity = item.get("id")
                            if identity not in seen_tools:
                                seen_tools.add(identity)
                                if not tools.consume():
                                    stop("Shared tool event budget exhausted")
                        if event.get("type") == "turn.completed":
                            for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
                                usage[key] += event.get("usage", {}).get(key, 0)
                    process.wait()
                except BaseException:
                    stop("Worker interrupted")
                    process.wait()
                    raise
                finally:
                    timer.cancel()
                    process.stdout.close()
            usage["elapsed_seconds"] = time.monotonic() - started
            atomic_json(output_dir / "usage.json", usage)
            if failure or process.returncode or not result_file.exists():
                raise ProviderError(f"{task['id']}: {failure or 'Codex failed; inspect local stderr.log'}")
            return {"result": json.loads(result_file.read_text()), "usage": usage}


class FixtureProvider:
    """Deterministic integration demo. Never manufactures housing observations."""

    name = "fixture"

    def check(self) -> str:
        return "Offline fixture; no model calls or real property findings"

    def run(self, task: dict, prompt: str, output_dir: Path, timeout: float, tools: ToolBudget) -> dict:
        result = {
            "summary": f"[데모] {task['role']} / {task['phase']} 실행 확인. 실제 부동산 조사 결과 아님.",
            "hypotheses": ["[데모 가설] 원문과 비교 가능한 자료가 확보되어야 판단할 수 있다."],
            "evidence": [],
            "claims": [],
            "issues": [],
            "discussion_requests": [],
            "limitations": ["fixture 모드: 실제 원문 수집·가격 평가를 수행하지 않았습니다."],
        }
        if task["role"] == "market" and task["phase"] == "analysis":
            result["discussion_requests"] = [
                {
                    "target_roles": ["policy", "development"],
                    "question": "[데모] 공급 일정과 시행 정책의 기준일을 대조해 주세요.",
                    "claim_refs": [],
                }
            ]
        return {
            "result": result,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0, "billing": "fixture"},
        }
