"""Acceptance tests for misleading research states from the design draft."""

import pytest
from pydantic import ValidationError

from estate_harness.schemas import (
    AgentResult,
    ClaimRecord,
    DiscussionRequest,
    EvidenceRecord,
    IssueRecord,
    PolicyEvent,
)
from estate_harness.validation import validate_result

AS_OF = "2026-09-07"


def evidence(**changes):
    return EvidenceRecord(
        **(
            {
                "evidence_id": "ev-market-001",
                "original_publisher": "국토교통부",
                "original_url": "https://www.molit.go.kr/example",
                "retrieved_at": "2026-09-07T10:00:00+09:00",
                "published_at": "2026-09-06",
                "observation_period": "2026-08",
                "revision_id": "2026-09-06-v1",
                "locator": "표 1, 지역 코드 11650, 2026년 8월 계약",
                "source_family_id": "molit-transactions-202608",
                "status": "verified",
            }
            | changes
        )
    )


def numeric_claim(**changes):
    return ClaimRecord(
        **(
            {
                "claim_id": "claim-market-001",
                "statement": "해당 면적의 거래 중앙값은 900000000 KRW이다.",
                "claim_type": "observation",
                "geography_id": "11650",
                "complex_id": "fixture-complex",
                "area_sqm": 59,
                "period": "2026-08",
                "unit": "KRW",
                "statistic": "median completed sale price; n=10",
                "evidence_refs": ["ev-market-001"],
                "status": "verified",
            }
            | changes
        )
    )


def result(**changes):
    return AgentResult(
        **(
            {
                "summary": "검증 테스트용 결과",
                "hypotheses": ["지역의 임대료 상승이 거래가격에 전달되는지 반증한다."],
            }
            | changes
        )
    )


def test_valid_observation_with_full_dimensions_and_ledger_reference():
    output = result(claims=[numeric_claim()])
    assert validate_result(output, "market", AS_OF, [evidence()]) == []


@pytest.mark.parametrize("field", ["geography_id", "period", "unit", "statistic", "area_sqm"])
def test_verified_numerical_claim_requires_comparable_dimensions(field):
    output = result(evidence=[evidence()], claims=[numeric_claim(**{field: None})])
    assert any(field in error for error in validate_result(output, "market", AS_OF))


def test_macro_observation_does_not_require_a_complex_or_area():
    claim = numeric_claim(complex_id=None, area_sqm=None, statistic="national index")
    assert validate_result(result(evidence=[evidence()], claims=[claim]), "market", AS_OF) == []


def test_user_inputs_are_not_external_observations():
    claim = ClaimRecord(
        claim_id="user-budget",
        statement="사용자가 가격 상한 1100000000 KRW을 제시했다.",
        claim_type="user_input",
        status="verified",
    )
    assert validate_result(result(claims=[claim]), "decision", AS_OF) == []


def test_unknown_evidence_reference_is_not_accepted_even_when_claim_unverified():
    output = result(claims=[numeric_claim(status="unverified")])
    assert any("missing evidence reference" in item for item in validate_result(output, "market", AS_OF))


def test_unverified_source_cannot_support_verified_claim():
    output = result(evidence=[evidence(status="unverified")], claims=[numeric_claim()])
    assert any("not verified" in item for item in validate_result(output, "market", AS_OF))


def test_verified_observation_needs_references():
    output = result(claims=[numeric_claim(evidence_refs=[])])
    assert any("requires evidence references" in item for item in validate_result(output, "market", AS_OF))


def test_calculation_requires_an_artifact_reference():
    output = result(claims=[numeric_claim(claim_type="calculation", evidence_refs=[])])
    assert any("calculation_ref" in item for item in validate_result(output, "decision", AS_OF))
    output.claims[0].calculation_ref = "calculations/fixture-cashflow-v1.json"
    assert validate_result(output, "decision", AS_OF) == []


@pytest.mark.parametrize("field", ["published_at", "first_seen_at", "retrieved_at"])
def test_future_evidence_is_excluded_from_backtest(field):
    output = result(evidence=[evidence(**{field: "2026-09-08"})])
    assert any(
        f"{field} is after research_as_of" in item for item in validate_result(output, "evidence", AS_OF)
    )


def test_reused_future_ledger_evidence_does_not_bypass_time_gate():
    output = result(claims=[numeric_claim()])
    errors = validate_result(output, "market", AS_OF, [evidence(published_at="2026-09-08")])
    assert any("after research_as_of" in item for item in errors)


def test_seoul_cutoff_accounts_for_utc_timestamps():
    before = evidence(retrieved_at="2026-09-07T14:59:59Z")
    after = evidence(retrieved_at="2026-09-07T15:00:00Z")
    assert validate_result(result(evidence=[before]), "evidence", AS_OF) == []
    assert any(
        "after research_as_of" in item
        for item in validate_result(result(evidence=[after]), "evidence", AS_OF)
    )


def test_invalid_dates_and_missing_availability_dates_are_blocked():
    output = result(evidence=[evidence(retrieved_at="yesterday")])
    assert any("ISO" in item for item in validate_result(output, "evidence", AS_OF))
    output = result(evidence=[evidence(published_at=None, first_seen_at=None)])
    assert any("published_at or first_seen_at" in item for item in validate_result(output, "evidence", AS_OF))
    assert any("research_as_of" in item for item in validate_result(result(), "evidence", "unknown"))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.org/data.csv",
        "https://localhost/data",
        "http://127.0.0.1/data",
        "http://169.254.169.254/latest",
        "http://[::1]/data",
        "https://user:password@example.org/data",
        "https://service.internal/data",
    ],
)
def test_unsafe_source_urls_are_rejected(url):
    output = result(evidence=[evidence(original_url=url)])
    assert any("public HTTP(S)" in item for item in validate_result(output, "evidence", AS_OF))


def test_existing_evidence_cannot_silently_change_under_same_id():
    output = result(evidence=[evidence(revision_id="v2")])
    assert any(
        "cannot be overwritten" in item for item in validate_result(output, "evidence", AS_OF, [evidence()])
    )


@pytest.mark.parametrize("role", ["policy", "market", "development"])
def test_analysts_require_independent_hypotheses(role):
    assert any("hypotheses" in item for item in validate_result(result(hypotheses=[]), role, AS_OF))


@pytest.mark.parametrize(
    "statement",
    [
        "상승 확률은 70%다.",
        "70% 확률로 사업이 성공한다.",
        "Probability: 0.7",
        "개발 성공 가능성 80퍼센트",
        "상승 확률은 1이다.",
    ],
)
def test_probability_numbers_in_prose_are_blocked(statement):
    assert any(
        "probabilities are disabled" in item
        for item in validate_result(result(summary=statement), "decision", AS_OF)
    )


def test_interest_and_price_scenario_percentages_remain_allowed():
    output = result(summary="금리 3%와 가격 -10%는 조건부 시나리오이며 실제 확률은 정하지 않는다.")
    assert validate_result(output, "decision", AS_OF) == []


def test_probability_fields_cannot_be_smuggled_into_json():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentResult.model_validate({"summary": "가정", "probability": 0.7})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClaimRecord.model_validate(
            {
                "claim_id": "forecast-1",
                "statement": "상승할 것이다.",
                "claim_type": "forecast",
                "probability": 0.7,
            }
        )


def test_resolved_issue_requires_actual_resolution_reason():
    issue = IssueRecord(
        issue_id="issue-1", issue_type="source_error", severity="major", owner="evidence", status="resolved"
    )
    assert any(
        "resolution_reason" in item for item in validate_result(result(issues=[issue]), "auditor", AS_OF)
    )


def test_open_major_issue_blocks_affected_verified_claim():
    issue = IssueRecord(
        issue_id="issue-1",
        affected_claim_ids=["claim-market-001"],
        issue_type="data_confusion",
        severity="major",
        owner="market",
    )
    output = result(evidence=[evidence()], claims=[numeric_claim()], issues=[issue])
    assert any("blocked by open major issue" in item for item in validate_result(output, "auditor", AS_OF))


def test_duplicate_ids_and_empty_discussion_routing_are_rejected():
    output = result(
        evidence=[evidence(), evidence()], discussion_requests=[DiscussionRequest(question="원문 확인 필요")]
    )
    errors = validate_result(output, "auditor", AS_OF)
    assert any("duplicate evidence ID" in item for item in errors)
    assert any("target role" in item for item in errors)


def test_discussion_can_reference_a_peer_claim_but_only_known_roles():
    request = DiscussionRequest(
        target_roles=["market", "auditor"], question="기간 비교를 확인해 주세요.", claim_refs=["peer-claim"]
    )
    assert validate_result(result(discussion_requests=[request]), "policy", AS_OF) == []
    with pytest.raises(ValidationError):
        DiscussionRequest(target_roles=["external-person"], question="확인 요청")


def test_lists_are_not_shared_between_agent_results():
    first, second = result(), result()
    first.limitations.append("원문 도구 없음")
    assert second.limitations == []


def test_policy_event_keeps_proposal_separate_from_effective_status():
    proposal = PolicyEvent(
        policy_id="policy-proposal", instrument="거래세", target_group="가구", status="proposed"
    )
    assert proposal.effective_date is None
    assert proposal.status != "effective"
