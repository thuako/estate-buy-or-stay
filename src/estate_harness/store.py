from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Store:
    """Only the controller writes. SQLite is the canonical state, files are exports."""

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "state.sqlite", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS records (kind TEXT, key TEXT, data TEXT, PRIMARY KEY(kind,key))"
        )

    def get(self, kind: str, key: str, default=None):
        row = self.db.execute("SELECT data FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, kind: str, key: str, value: object) -> None:
        self.db.execute(
            "INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,key) DO UPDATE SET data=excluded.data",
            (kind, key, canonical(value)),
        )

    def all(self, kind: str) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT data FROM records WHERE kind=? ORDER BY key", (kind,))
        ]

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @contextmanager
    def controller_lock(self):
        with (self.directory / "controller.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another controller is already operating this run") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def export(self) -> None:
        for kind in ("run", "task", "evidence", "claim", "finding", "discussion", "usage"):
            atomic_json(self.directory / f"{kind}.json", self.all(kind))
