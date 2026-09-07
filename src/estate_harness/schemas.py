"""Typed, versionable research contracts shared by every agent.

Provider output is deliberately narrow: agents propose records and discussion
requests; only the controller may commit state or write to GitHub.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROLES = ("orchestrator", "evidence", "policy", "market", "development", "decision", "auditor")
Role = Literal["orchestrator", "evidence", "policy", "market", "development", "decision", "auditor"]
ClaimType = Literal[
    "observation", "user_input", "calculation", "causal_interpretation", "forecast", "decision"
]


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceRecord(ResearchModel):
    evidence_id: str = Field(min_length=1)
    original_publisher: str = Field(min_length=1)
    original_url: str = Field(min_length=1)
    retrieved_at: str = Field(min_length=1)
    published_at: str | None = None
    observation_period: str = Field(min_length=1)
    first_seen_at: str | None = None
    revision_id: str = Field(min_length=1)
    content_hash: str | None = None
    locator: str = Field(min_length=1)
    source_family_id: str = Field(min_length=1)
    coverage_limitations: list[str] = Field(default_factory=list)
    status: Literal["verified", "unverified", "unavailable"] = "unverified"


class ClaimRecord(ResearchModel):
    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    claim_type: ClaimType
    geography_id: str | None = None
    complex_id: str | None = None
    area_sqm: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    period: str | None = None
    unit: str | None = None
    statistic: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    calculation_ref: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    counterevidence_refs: list[str] = Field(default_factory=list)
    status: Literal["unverified", "verified", "disputed", "stale"] = "unverified"


class IssueRecord(ResearchModel):
    issue_id: str = Field(min_length=1)
    affected_claim_ids: list[str] = Field(default_factory=list)
    issue_type: str = Field(min_length=1)
    severity: Literal["critical", "major", "minor"]
    competing_explanations: list[str] = Field(default_factory=list)
    requested_evidence: list[str] = Field(default_factory=list)
    owner: Role
    status: Literal["open", "resolved"] = "open"
    resolution_reason: str | None = None


class DiscussionRequest(ResearchModel):
    target_roles: list[Role] = Field(default_factory=list)
    question: str = Field(min_length=1)
    claim_refs: list[str] = Field(default_factory=list)


class AgentResult(ResearchModel):
    summary: str = Field(min_length=1)
    hypotheses: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    issues: list[IssueRecord] = Field(default_factory=list)
    discussion_requests: list[DiscussionRequest] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PolicyEvent(ResearchModel):
    """Optional collector contract; distinct dates prevent proposal/enactment confusion."""

    policy_id: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    target_group: str = Field(min_length=1)
    proposal_date: str | None = None
    announcement_date: str | None = None
    enactment_date: str | None = None
    effective_date: str | None = None
    expiry_date: str | None = None
    status: Literal["proposed", "announced", "enacted", "effective", "withdrawn", "expired"]
    supersedes_policy_id: str | None = None
    expected_channels: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
