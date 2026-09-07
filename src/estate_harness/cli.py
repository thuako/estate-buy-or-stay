from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from estate_harness.adapters.codex import CodexProvider, FixtureProvider
from estate_harness.adapters.github import FixtureIssues, GitHubIssues
from estate_harness.config import read_config, read_json
from estate_harness.store import Store
from estate_harness.workflow import Harness


def parser():
    root = argparse.ArgumentParser(description="GitHub Issues 기반 부동산 multi-agent research harness")
    root.add_argument("--config", type=Path, default=Path("config/harness.toml"))
    root.add_argument("--runs-dir", type=Path, default=Path("runs"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="GitHub API 및 Codex 인증 확인")
    for name in ("start", "demo"):
        cmd = commands.add_parser(
            name, help="새 연구 시작" if name == "start" else "API 호출 없는 전체 흐름 데모"
        )
        cmd.add_argument("--run-id", required=True)
        cmd.add_argument("--profile", type=Path, default=Path("config/profile.json"))
        cmd.add_argument("--roles-dir", type=Path, default=Path("roles"))
        cmd.add_argument(
            "--question",
            default="2027년 4월 전후 11억 이하 후보의 매수와 임대 유지 조건을 비교하고, "
            "현재 가격의 24·36개월 기본가치 선반영과 판단 변경 조건을 검증하라.",
        )
        cmd.add_argument("--scenario", type=Path)
        if name == "start":
            cmd.add_argument(
                "--plan-only", action="store_true", help="GitHub 작업 이슈만 생성, 모델 실행은 보류"
            )
    for name in ("resume", "sync", "watch", "status", "invalidate"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--run-id", required=True)
        if name == "watch":
            cmd.add_argument("--cycles", type=int, default=10, help="제한된 횟수만 폴링; 기본 10회")
        if name == "invalidate":
            cmd.add_argument("--evidence-id", required=True)
            cmd.add_argument("--reason", required=True)
    calc = commands.add_parser("calculate")
    calc.add_argument("scenario", type=Path)
    collect = commands.add_parser("collect-source", help="원문 응답과 hash/취득상태 manifest 보존")
    collect.add_argument("url")
    collect.add_argument("--output-dir", type=Path, default=Path("runs/sources"))
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "collect-source":
            from estate_harness.collectors.snapshots import collect_source

            manifest = collect_source(args.url, args.output_dir)
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 0 if manifest["status"] == "retrieved" else 1
        if args.command == "calculate":
            from estate_harness.calculators import calculate_scenario

            print(json.dumps(calculate_scenario(read_json(args.scenario)), ensure_ascii=False, indent=2))
            return 0
        config = read_config(args.config)
        if args.command == "doctor":
            checks = {}
            for name, check in (
                ("github", GitHubIssues(config.repository).check_access),
                ("codex", CodexProvider(config.model).check),
            ):
                try:
                    checks[name] = {"ok": True, "detail": check()}
                except Exception as error:
                    checks[name] = {"ok": False, "detail": str(error)}
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            return 0 if all(check["ok"] for check in checks.values()) else 1
        # Reject path traversal before opening any state directory.
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", args.run_id):
            raise ValueError("Invalid run id")
        store = Store(args.runs_dir / args.run_id)
        with store.controller_lock():
            existing = store.get("run", "current")
            if args.command not in ("start", "demo") and not existing:
                raise ValueError("Unknown run; use start or demo first")
            if existing:
                from estate_harness.config import Config

                config = Config.model_validate(existing["config"])
            fixture = args.command == "demo" or (existing and existing["provider"] == "fixture")
            github = (
                FixtureIssues(store.directory / "github-fixture.json", repo=config.repository)
                if fixture
                else GitHubIssues(config.repository)
            )
            provider = FixtureProvider() if fixture else CodexProvider(config.model)
            harness = Harness(store, github, provider)
            if args.command in ("start", "demo"):
                harness.initialize(
                    args.run_id,
                    config,
                    read_json(args.profile),
                    args.question,
                    args.roles_dir,
                    read_json(args.scenario) if args.scenario else None,
                )
                if not getattr(args, "plan_only", False):
                    harness.execute()
            elif args.command == "resume":
                harness.execute()
            elif args.command == "sync":
                print(f"등록한 태그 작업: {harness.sync()}")
                harness.store.export()
            elif args.command == "watch":
                if args.cycles < 1:
                    raise ValueError("cycles must be positive")
                for index in range(args.cycles):
                    added = harness.sync()
                    if added or harness.run["status"] in ("queued", "running"):
                        harness.execute(sync=False)
                    if index + 1 < args.cycles:
                        time.sleep(config.poll_seconds)
            elif args.command == "invalidate":
                print(json.dumps(harness.invalidate(args.evidence_id, args.reason), ensure_ascii=False))
            run = harness.run
            print(
                json.dumps(
                    {
                        "run_id": run["id"],
                        "status": run["status"],
                        "agent_calls": run["agent_calls"],
                        "tool_calls": run["tool_calls"],
                        "report": str(store.directory / "report.md"),
                        "github": store.get("issue", "run")
                        if not fixture
                        else {
                            "mode": "offline_fixture",
                            "file": str(store.directory / "github-fixture.json"),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"estate-harness: {type(error).__name__}: {error}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
