from __future__ import annotations

import json


def render_report(store) -> str:
    run = store.get("run", "current")
    tasks = store.all("task")
    decision = store.get("task", f"decision-{run['revision']}") or {}
    audit = store.get("task", f"audit-{run['revision']}") or {}
    result = decision.get("result") or {}
    audit_result = audit.get("result") or {}
    lines = [
        f"# 부동산 연구 {run['id']}",
        "",
        f"기준일: {run['config']['research_as_of']}",
        f"상태: **{run['status']}** · 실행 모드: `{run['provider']}`",
        "",
        run["question"],
        "",
        "## 판단",
        "",
        result.get("summary", "의사결정 작업 미완료: 최종 매수 판단을 보류합니다."),
        "",
        "## 검증과 반대 근거",
        "",
        audit_result.get("summary", "최종 감사 미완료."),
        "",
    ]
    for issue in store.all("finding"):
        lines.append(
            f"- [{issue['severity']} / {issue['status']}] {issue['issue_type']}: "
            f"{issue.get('resolution_reason') or '; '.join(issue['requested_evidence'])}"
        )
    lines.extend(["", "## 가계 조건", ""])
    required = (
        "liquid_assets_krw",
        "annual_household_income_krw",
        "lender_confirmed_borrowing_capacity",
        "deposit_available_date",
        "monthly_housing_payment_limit_krw",
    )
    missing = [key for key in required if run["profile"].get(key) is None]
    lines.append(
        "가계 매수 가능 여부: **미판정**. 미확인 입력: " + ", ".join(missing)
        if missing
        else "가계 입력은 제공됨. 계산 결과와 개별 금융기관의 적용일·심사를 함께 확인해야 합니다."
    )
    lines.extend(["", "## 수급·기본가치·전략 계산", ""])
    if run.get("calculations"):
        lines.extend(
            [
                "아래 결과는 입력된 가정에 따른 계산입니다.",
                "",
                "```json",
                json.dumps(run["calculations"], ensure_ascii=False, indent=2),
                "```",
            ]
        )
    else:
        lines.append(
            "재계산 가능한 scenario 입력 미확보. 현재·24·36개월 기본가치, 선반영 기간, "
            "매수·임대 현금흐름과 손익분기 가격은 미산정입니다."
        )
    lines.extend(
        ["", "## 주장 원장", "", "| ID | 유형 | 상태 | 주장 | 근거 |", "| --- | --- | --- | --- | --- |"]
    )
    for claim in store.all("claim"):
        statement = claim["statement"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {claim['claim_id']} | {claim['claim_type']} | {claim['status']} | "
            f"{statement} | {', '.join(claim['evidence_refs'])} |"
        )
    if not store.all("claim"):
        lines.append("| — | — | 미확인 | 검증된 부동산 주장이 없습니다. | — |")
    lines.extend(["", "## 원문 근거", ""])
    for evidence in store.all("evidence"):
        lines.append(
            f"- {evidence['evidence_id']} · [{evidence['original_publisher']}]"
            f"({evidence['original_url']}) · {evidence['status']} · {evidence['locator']}"
        )
    lines.extend(["", "## 미해결·한계", ""])
    limits = list(run["limits"])
    for task in tasks:
        if task["status"] not in ("done", "superseded"):
            limits.append(f"{task['id']}: {task['status']} — {task.get('error', '미완료')}")
        if task["result"]:
            limits.extend(task["result"]["limitations"])
    lines.extend(f"- {text}" for text in dict.fromkeys(limits))
    lines.extend(["", "## 다음 확인 항목 (최대 3개)", ""])
    questions = [
        discussion["question"] for discussion in store.all("discussion") if discussion["status"] == "open"
    ]
    questions.extend(f"가계 입력 확인: {key}" for key in missing)
    lines.extend(f"- {question}" for question in list(dict.fromkeys(questions))[:3])
    lines.extend(
        [
            "",
            "## 실행 기록",
            "",
            f"에이전트 호출: {run['agent_calls']} / "
            f"{run['config']['max_agent_calls']} · 관측 도구 호출: {run['tool_calls']} / "
            f"{run['config']['max_tool_calls']} · 누적 실행 시간: {run['elapsed_seconds']:.1f}초",
            f"코드 버전: `{run['code_version']}` · 재검토 라운드: {run['revision']}",
            "",
            "모델 비용: fixture는 0; Codex 계정 사용량의 USD 비용은 미확인. 토큰·시도 기록은 usage.json.",
            "근거 메타데이터·가정·프롬프트·실행 이벤트와 복구 상태는 로컬 run 디렉터리에 보존됩니다. "
            "원문 파일은 collect-source 명령으로 별도 보존할 수 있습니다.",
        ]
    )
    return "\n".join(lines) + "\n"
