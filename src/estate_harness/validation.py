"""Deterministic acceptance gates; these do not prove factual correctness."""

import ipaddress
import re
from collections import Counter
from datetime import UTC, date, datetime, time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .schemas import ROLES, AgentResult, EvidenceRecord

_SEOUL = ZoneInfo("Asia/Seoul")
_NUMBER = re.compile(r"\d")
# Price changes and interest rates remain permitted. Quantitative probabilities
# are disabled even when disguised as prose rather than an extra JSON field.
_PROBABILITY = re.compile(
    r"(?:확률|가능성|probabilit(?:y|ies)|likelihood|chance)"
    r"\s*(?:은|는|이|가|of|is|:|=|약|약\s*|대략|about|approximately)*\s*"
    r"(?:\d+(?:\.\d+)?\s*(?:%|퍼센트|percent)?)"
    r"|\d+(?:\.\d+)?\s*(?:%|퍼센트|percent)\s*(?:의\s*)?"
    r"(?:확률|가능성|probabilit(?:y|ies)|likelihood|chance)",
    re.IGNORECASE,
)


def _timestamp(value: str, *, end_of_day: bool = False) -> datetime:
    """Dates and naive datetimes follow the research profile's Seoul timezone."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = datetime.combine(
            date.fromisoformat(value), time.max if end_of_day else time.min, tzinfo=_SEOUL
        )
    else:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_SEOUL)
    return parsed.astimezone(UTC)


def _public_source_url(value: str) -> bool:
    """Reject unsafe locator schemes and obvious local/network metadata targets.

    This is a record gate, not a network fetcher or DNS rebinding defense.
    """
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"https", "http"} or not host:
            return False
        if parsed.username or parsed.password or any(char.isspace() for char in value):
            return False
        if host.lower().rstrip(".") in {"localhost", "metadata.google.internal"}:
            return False
        if host.lower().rstrip(".").endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def validate_result(
    result: AgentResult,
    role: str,
    research_as_of: str,
    known_evidence: list[EvidenceRecord] | None = None,
) -> list[str]:
    """Return blocking violations without changing the submitted research.

    References may resolve against the shared evidence ledger. Claim references
    in discussions/issues can refer to peer output; the controller owns that
    cross-task lookup. Verified denotes a claimed verification state and still
    requires source inspection by the independent auditor.
    """
    errors: list[str] = []
    if role not in ROLES:
        errors.append(f"unknown role: {role}")
    if role in {"policy", "market", "development"} and not any(item.strip() for item in result.hypotheses):
        errors.append(f"{role}: independent hypotheses are required before analysis")
    try:
        cutoff = _timestamp(research_as_of, end_of_day=True)
    except (ValueError, TypeError):
        return errors + ["research_as_of must be an ISO date or datetime"]

    for name, records, key in (
        ("evidence", result.evidence, "evidence_id"),
        ("claims", result.claims, "claim_id"),
        ("issues", result.issues, "issue_id"),
    ):
        for identifier, count in Counter(getattr(record, key) for record in records).items():
            if count > 1:
                errors.append(f"duplicate {name} ID: {identifier}")

    evidence_by_id = {record.evidence_id: record for record in known_evidence or []}
    for record in result.evidence:
        prior = evidence_by_id.get(record.evidence_id)
        if prior is not None and prior != record:
            errors.append(f"{record.evidence_id}: an existing evidence ID cannot be overwritten")
        evidence_by_id[record.evidence_id] = record

    # Validate cited ledger records as well: an old unverified/future snapshot
    # must not become acceptable simply because another task registered it.
    referenced = {
        reference for claim in result.claims for reference in claim.evidence_refs + claim.counterevidence_refs
    }
    records_to_check = {record.evidence_id: record for record in result.evidence} | {
        identifier: evidence_by_id[identifier] for identifier in referenced if identifier in evidence_by_id
    }
    for record in records_to_check.values():
        if not _public_source_url(record.original_url):
            errors.append(f"{record.evidence_id}: source URL must be public HTTP(S)")
        timestamps: dict[str, datetime] = {}
        for field in ("retrieved_at", "published_at", "first_seen_at"):
            value = getattr(record, field)
            if value is None:
                continue
            try:
                timestamps[field] = _timestamp(value)
            except (ValueError, TypeError):
                errors.append(f"{record.evidence_id}: {field} must be an ISO date or datetime")
                continue
            if timestamps[field] > cutoff:
                errors.append(f"{record.evidence_id}: {field} is after research_as_of")
        retrieved = timestamps.get("retrieved_at")
        published = timestamps.get("published_at")
        if retrieved and published and published > retrieved:
            errors.append(f"{record.evidence_id}: published_at is after retrieved_at")
        if record.status == "verified" and not (record.published_at or record.first_seen_at):
            errors.append(f"{record.evidence_id}: verified evidence needs published_at or first_seen_at")

    for claim in result.claims:
        for reference in claim.evidence_refs + claim.counterevidence_refs:
            if reference not in evidence_by_id:
                errors.append(f"{claim.claim_id}: missing evidence reference {reference}")
        if claim.status != "verified":
            continue
        if claim.claim_type == "calculation" and not claim.calculation_ref:
            errors.append(f"{claim.claim_id}: verified calculation requires calculation_ref")
        if claim.claim_type not in {"user_input", "calculation"} and not claim.evidence_refs:
            errors.append(f"{claim.claim_id}: verified claim requires evidence references")
        for reference in claim.evidence_refs:
            record = evidence_by_id.get(reference)
            if record is not None and record.status != "verified":
                errors.append(f"{claim.claim_id}: cited evidence {reference} is not verified")
        if claim.claim_type != "user_input" and _NUMBER.search(claim.statement):
            missing = [
                field
                for field in ("geography_id", "period", "unit", "statistic")
                if not getattr(claim, field)
            ]
            if claim.complex_id and claim.area_sqm is None:
                missing.append("area_sqm")
            if missing:
                errors.append(
                    f"{claim.claim_id}: verified numerical claim missing dimensions: " + ", ".join(missing)
                )
        if claim.claim_type in {"causal_interpretation", "forecast", "decision"} and not claim.assumptions:
            errors.append(f"{claim.claim_id}: {claim.claim_type} requires explicit assumptions")

    for issue in result.issues:
        if issue.status == "resolved" and not (issue.resolution_reason or "").strip():
            errors.append(f"{issue.issue_id}: resolved issue requires resolution_reason")
        if issue.status == "open" and issue.severity in {"critical", "major"}:
            for claim in result.claims:
                if claim.claim_id in issue.affected_claim_ids and claim.status == "verified":
                    errors.append(
                        f"{claim.claim_id}: blocked by open {issue.severity} issue {issue.issue_id}"
                    )
    for request in result.discussion_requests:
        if not request.target_roles:
            errors.append("discussion request requires at least one target role")
        if len(request.target_roles) != len(set(request.target_roles)):
            errors.append("discussion request contains duplicate target roles")

    # Inspect all prose, including summaries/limitations, so probability output
    # cannot bypass the schema merely by being moved outside ClaimRecord.
    def inspect_text(value: object) -> None:
        if isinstance(value, str) and _PROBABILITY.search(value):
            errors.append("quantitative probabilities are disabled; use conditional scenarios")
        elif isinstance(value, dict):
            for nested in value.values():
                inspect_text(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect_text(nested)

    inspect_text(result.model_dump())
    return list(dict.fromkeys(errors))
