import hashlib
import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from estate_harness.adapters.codex import ToolBudget, strict_schema
from estate_harness.collectors.snapshots import collect_source
from estate_harness.schemas import AgentResult


def test_shared_tool_budget_is_atomic():
    budget = ToolBudget(7)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: budget.consume(), range(80)))
    assert sum(results) == 7
    assert budget.remaining == 0
    assert budget.used == 7


def test_codex_schema_requires_all_keys_and_preserves_nullable_fields():
    original = AgentResult.model_json_schema()
    schema = strict_schema(original)
    assert "default" in original["$defs"]["EvidenceRecord"]["properties"]["published_at"]
    assert {"type": "null"} in schema["$defs"]["EvidenceRecord"]["properties"]["published_at"]["anyOf"]

    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert set(value["required"]) == set(value["properties"])
                assert value["additionalProperties"] is False
            assert "default" not in value
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(schema)


def test_snapshot_preserves_original_bytes_and_metadata(tmp_path):
    raw = "원표 자료\r\n1,200\r\n".encode()
    response = io.BytesIO(raw)
    response.url = "https://example.org/source"
    response.headers = {"Content-Type": "text/csv", "ETag": '"revision-2"'}
    with (
        patch("estate_harness.collectors.snapshots.check_public_url"),
        patch("urllib.request.build_opener") as opener,
    ):
        opener.return_value.open.return_value = response
        manifest = collect_source("https://example.org/source", tmp_path)
    assert manifest["status"] == "retrieved"
    assert manifest["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert (tmp_path / (manifest["content_hash"] + ".bin")).read_bytes() == raw
    assert manifest["published_at"] is None
    assert manifest["revision_id"] == '"revision-2"'
    assert json.loads(next(tmp_path.glob("*.manifest.json")).read_text()) == manifest


@pytest.mark.parametrize(
    ("http_status", "expected"), [(404, "not_found"), (403, "unavailable"), (503, "unavailable")]
)
def test_failed_source_is_not_zero_observations(tmp_path, http_status, expected):
    with (
        patch("estate_harness.collectors.snapshots.check_public_url"),
        patch("urllib.request.build_opener") as opener,
    ):
        opener.return_value.open.side_effect = urllib.error.HTTPError(
            "https://example.org/source", http_status, "failure", {}, None
        )
        manifest = collect_source("https://example.org/source", tmp_path)
    assert manifest["status"] == expected
    assert manifest["content_hash"] is None
    assert manifest["path"] is None
    assert not list(tmp_path.glob("*.bin"))
