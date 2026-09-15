"""Tools the agent may call.

Three groups, and the agent's instruction says which one a request needs:

* Readers over a stored evaluation run, and over the policy in force now.
  None of them computes anything: the numbers were fixed when the run was
  written, and the agent's job is to explain them, not to re-derive them. If
  the agent could recalculate, it could disagree with the dashboard, and then
  neither would be trustworthy.
* simulate_what_if - the real engine on the real quotes under hypothetical
  policy, with nothing saved.
* apply_changes, add_compliance_requirement, rerun_evaluation - the buyer
  asked for policy to change, so it changes and a new immutable run records
  the result. Past runs are never touched.

simulate_what_if and apply_changes take the same arguments, parsed by the same
reference_action.parse_changes, so what was simulated is exactly what gets
applied. Before-and-after comparisons are worked out here in Python; the
agent reports them and does no arithmetic of its own.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from decimal import Decimal
from dataclasses import dataclass, field

from ..db import SessionLocal
from ..models import EvaluationRun


@dataclass
class TurnLog:
    """What the agent did during one request, beyond reading.

    The HTTP layer needs this to link the new run and to offer "Apply this
    change" under a simulation, and it has to survive the agent timing out
    after a change was already saved - which is why it is recorded as the
    tools run rather than read back from the model's reply.
    """
    actions: list[dict] = field(default_factory=list)
    new_run_ids: list[str] = field(default_factory=list)
    last_simulation: dict | None = None


_turn: contextvars.ContextVar[TurnLog | None] = contextvars.ContextVar(
    "sourcing_agent_turn", default=None)


@contextmanager
def record_turn():
    """Collect the agent's actions for the duration of the block.

    A mutable log in a context variable: asyncio tasks and worker threads copy
    the context, but a copy still points at the same log. Tools called with no
    recorder active - the A2A surface - simply do not record.
    """
    log = TurnLog()
    token = _turn.set(log)
    try:
        yield log
    finally:
        _turn.reset(token)


def _record(**action) -> None:
    log = _turn.get()
    if log is None:
        return
    log.actions.append(action)
    if action.get("new_run_id"):
        log.new_run_ids.append(action["new_run_id"])


def _run(run_id: str) -> dict:
    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        if not run:
            return {}
        return run.result or {}


def get_run_summary(run_id: str) -> dict:
    """Headline figures and the final ranking for one evaluation run.

    Args:
        run_id: identifier of the evaluation run.
    """
    result = _run(run_id)
    if not result:
        return {"error": "no such run"}
    return {
        "kpis": result.get("kpis", {}),
        "ceiling_equivalent_total_eur": result.get("ceiling_equivalent_total_eur"),
        "suppliers": [
            {
                "supplier_id": s["supplier_id"],
                "supplier_name": s["supplier_name"],
                "total_landed_cost_eur": s["total_landed_cost_eur"],
                "base_rank": s.get("base_rank"),
                "final_rank": s.get("final_rank"),
                "award_status": s.get("award_status"),
                "primary_reason": s.get("primary_reason"),
                "payment_terms_net_days": s.get("payment_terms_net_days"),
                "lead_time_midpoint_weeks": s.get("lead_time_midpoint_weeks"),
                "eligible": s.get("eligible"),
                "failed_gate": s.get("failed_gate"),
                "incoterm": s.get("incoterm"),
                "freight_adj_pct": s.get("freight_adj_pct"),
                "discount_pct_applied": s.get("discount_pct_applied"),
                "is_approved_supplier": s.get("is_approved_supplier"),
                "is_incumbent": s.get("is_incumbent"),
                "historical_share_pct": s.get("historical_share_pct"),
            }
            for s in result.get("suppliers", [])
        ],
        "warnings": result.get("warnings", []),
    }


def get_gate_results(run_id: str, supplier_id: str = "") -> dict:
    """The Gate 1, 2 and 3 trail with measured values and thresholds.

    Args:
        run_id: identifier of the evaluation run.
        supplier_id: optional, restricts the answer to one supplier.
    """
    gates = _run(run_id).get("gates", {})
    if supplier_id:
        return {supplier_id: gates.get(supplier_id, [])}
    return gates


def get_promotion_detail(run_id: str) -> dict:
    """The promotion rule's four conditions, each evaluated separately.

    Args:
        run_id: identifier of the evaluation run.
    """
    return {"promotions": _run(run_id).get("promotions", [])}


def get_line_comparison(run_id: str, cas_no: str = "") -> dict:
    """Per-item landed prices, ceiling flags and historical variance.

    Args:
        run_id: identifier of the evaluation run.
        cas_no: optional CAS number to restrict the answer to one material.
    """
    lines = _run(run_id).get("lines", [])
    if cas_no:
        lines = [line for line in lines if line["cas_no"] == cas_no]
    return {"lines": lines}


def get_compliance(run_id: str, supplier_id: str = "") -> dict:
    """Compliance checklist per supplier, with the sentence quoted from the quote.

    Args:
        run_id: identifier of the evaluation run.
        supplier_id: optional, restricts the answer to one supplier.
    """
    result = _run(run_id)
    gates = result.get("gates", {})
    matrix = result.get("compliance_matrix", [])

    out = {}
    for supplier in result.get("suppliers", []):
        sid = supplier["supplier_id"]
        if supplier_id and sid != supplier_id:
            continue
        gate1 = (gates.get(sid) or [{}])[0]
        out[sid] = {
            "supplier_name": supplier["supplier_name"],
            "mandatory_passed": gate1.get("passed"),
            "mandatory_detail": gate1.get("detail", {}),
            "advisory_gaps": supplier.get("advisory_gaps", []),
            "checklist": [
                {
                    "code": row["code"],
                    "label": row["label"],
                    "tier": row["tier"],
                    "claimed": (row["suppliers"].get(sid) or {}).get("claimed"),
                    "evidence_text": (row["suppliers"].get(sid) or {}).get("evidence_text"),
                    "evidence_page": (row["suppliers"].get(sid) or {}).get("evidence_page"),
                }
                for row in matrix
            ],
        }
    return out


def get_commercial_terms(run_id: str) -> dict:
    """Payment terms, lead time, Incoterm, MOQ terms and discount structure.

    Args:
        run_id: identifier of the evaluation run.
    """
    return {
        "suppliers": [
            {
                "supplier_id": s["supplier_id"],
                "supplier_name": s["supplier_name"],
                "incoterm": s.get("incoterm"),
                "freight_adj_pct": s.get("freight_adj_pct"),
                "freight_basis": s.get("freight_basis"),
                "payment_terms_net_days": s.get("payment_terms_net_days"),
                "lead_time_min_weeks": s.get("lead_time_min_weeks"),
                "lead_time_max_weeks": s.get("lead_time_max_weeks"),
                "lead_time_midpoint_weeks": s.get("lead_time_midpoint_weeks"),
                "moq_terms": s.get("moq_terms", []),
                "discount_structure": s.get("discount_structure", []),
                "discount_condition_met": s.get("discount_condition_met"),
            }
            for s in _run(run_id).get("suppliers", [])
        ]
    }


def get_data_quality(run_id: str) -> dict:
    """Line items whose stated total does not match unit price times quantity.

    Args:
        run_id: identifier of the evaluation run.
    """
    result = _run(run_id)
    return {
        "issues": result.get("data_quality", []),
        "issue_count": result.get("kpis", {}).get("data_quality_issues"),
    }


def get_renegotiation_candidates(run_id: str) -> dict:
    """Line items priced above the category strategy ceiling, worst first.

    Args:
        run_id: identifier of the evaluation run.
    """
    return {"candidates": _run(run_id).get("renegotiation", [])}


def get_allocation(run_id: str) -> dict:
    """Award split, plus what each vendor already holds of historical spend.

    Args:
        run_id: identifier of the evaluation run.
    """
    result = _run(run_id)
    context = result.get("historical_context", {})
    return {
        "allocation": result.get("allocation", []),
        "concentration_threshold_pct": context.get("concentration_threshold_pct"),
        "share_today": context.get("incumbent_vendors", []),
        "total_historical_spend_eur": context.get("incumbent_spend_eur"),
    }


def get_historical_prices(run_id: str) -> dict:
    """The purchase price history each landed price is compared against.

    Args:
        run_id: identifier of the evaluation run.
    """
    return {
        "materials": (_run(run_id).get("historical_context") or {}).get("materials", []),
    }



# ---------------------------------------------------------------------------
# The policy in force now - not a past run's snapshot
# ---------------------------------------------------------------------------

def _plain(value) -> str | None:
    """A stored number as a plain string, without the column's trailing zeros."""
    if value is None:
        return None
    return f"{Decimal(str(value)).normalize():f}"


def get_current_policy() -> dict:
    """The rule thresholds and constants in force right now, with valid ranges.

    Distinct from get_run_summary, which reads what a past run was evaluated
    with. Read this before simulating or changing a threshold, so a change is
    made relative to the real current value rather than a guessed one.
    """
    from ..models import PolicyConfig
    from ..reference_action import NOT_USED_BY_ENGINE, POLICY_LIMITS

    with SessionLocal() as session:
        thresholds = []
        for row in session.query(PolicyConfig).order_by(PolicyConfig.key):
            lo, hi, whole = POLICY_LIMITS.get(row.key, (None, None, False))
            thresholds.append({
                "key": row.key,
                "value": _plain(row.value),
                "unit": row.unit,
                "description": row.description,
                "min": _plain(lo),
                "max": _plain(hi),
                "whole_number_only": whole,
                "used_by_evaluation": row.key not in NOT_USED_BY_ENGINE,
            })
        return {"thresholds": thresholds}


def get_current_materials() -> dict:
    """Each material's CAS number, ceiling price (EUR/L) and required volume
    (litres) in force right now.

    Use the CAS numbers from here when simulating or changing a ceiling price
    or a required volume.
    """
    from ..models import Benchmark, Demand, Material

    with SessionLocal() as session:
        ceilings = {b.cas_no: b for b in session.query(Benchmark)}
        volumes = {d.cas_no: d for d in session.query(Demand)}
        return {
            "materials": [
                {
                    "cas_no": m.cas_no,
                    "name": m.name,
                    "ceiling_price_eur_l": _plain(getattr(
                        ceilings.get(m.cas_no), "ceiling_price_eur_l", None)),
                    "target_price_eur_l": _plain(getattr(
                        ceilings.get(m.cas_no), "target_price_eur_l", None)),
                    "required_qty_l": _plain(getattr(
                        volumes.get(m.cas_no), "required_qty_l", None)),
                    "in_demand_basket": m.cas_no in volumes,
                }
                for m in session.query(Material).order_by(Material.name)
            ]
        }


def get_current_compliance() -> dict:
    """The compliance checklist in force right now: each code, its label and
    its tier. MANDATORY codes drive Gate 1 exclusion; ADVISORY codes drive the
    promotion rule's compliance condition."""
    from ..models import ComplianceRequirement

    with SessionLocal() as session:
        return {
            "requirements": [
                {"code": row.code, "label": row.label, "tier": row.tier}
                for row in session.query(ComplianceRequirement).order_by(
                    ComplianceRequirement.code)
            ]
        }


# ---------------------------------------------------------------------------
# Before and after - worked out here so the agent only has to report it
# ---------------------------------------------------------------------------

def _failed_gate_explanation(result: dict, supplier: dict) -> str | None:
    gate_no = supplier.get("failed_gate")
    if not gate_no:
        return None
    for gate in (result.get("gates") or {}).get(supplier["supplier_id"], []):
        if gate.get("gate_no") == gate_no:
            return (gate.get("detail") or {}).get("explanation")
    return None


_OUTCOME_KEYS = ("rank", "status", "total_landed_cost_eur", "failed_gate")


def _standing(result: dict) -> dict[str, dict]:
    return {
        s["supplier_id"]: {
            "supplier_name": s["supplier_name"],
            "rank": s.get("final_rank"),
            "status": s.get("award_status"),
            "total_landed_cost_eur": s.get("total_landed_cost_eur"),
            "failed_gate": s.get("failed_gate"),
            "reason": s.get("primary_reason"),
            "failed_gate_explanation": _failed_gate_explanation(result, s),
        }
        for s in result.get("suppliers", [])
    }


def compare_outcomes(before: dict, after: dict) -> dict:
    """Each supplier's rank, award status, landed cost and failed gate, before
    and after, plus whether the recommendation itself moved."""
    old, new = _standing(before), _standing(after)
    suppliers = []
    for supplier_id, now in new.items():
        was = old.get(supplier_id, {})
        row = {"supplier_id": supplier_id, "supplier_name": now["supplier_name"]}
        for key in _OUTCOME_KEYS:
            row[f"{key}_before"] = was.get(key)
            row[f"{key}_after"] = now[key]
        row["changed"] = any(
            row[f"{key}_before"] != row[f"{key}_after"] for key in _OUTCOME_KEYS)
        row["reason_after"] = now["reason"]
        if now["failed_gate_explanation"]:
            row["failed_gate_explanation_after"] = now["failed_gate_explanation"]
        suppliers.append(row)

    recommended_before = (before.get("kpis") or {}).get("recommended_supplier")
    recommended_after = (after.get("kpis") or {}).get("recommended_supplier")
    return {
        "recommended_before": recommended_before,
        "recommended_after": recommended_after,
        "recommendation_changed": recommended_before != recommended_after,
        "anything_changed": any(s["changed"] for s in suppliers),
        "suppliers": suppliers,
        "promotions_after": [
            {
                "candidate_supplier_id": p["candidate_supplier_id"],
                "cheaper_supplier_id": p["cheaper_supplier_id"],
                "promoted": p["promoted"],
                "cost_gap_pct": p.get("cost_gap_pct"),
                "cost_condition_met": p["cost_condition_met"],
                "compliance_condition_met": p["compliance_condition_met"],
                "payment_condition_met": p["payment_condition_met"],
                "lead_time_condition_met": p["lead_time_condition_met"],
            }
            for p in after.get("promotions", [])
        ],
        "warnings_after": after.get("warnings", []),
    }


def _same_outcome(a: dict, b: dict) -> bool:
    left, right = _standing(a), _standing(b)
    return left.keys() == right.keys() and all(
        left[s][key] == right[s][key] for s in left for key in _OUTCOME_KEYS)


SCOPE_NOTE = (
    "Policy is shared: this applies to every future evaluation, in every "
    "comparison. Runs already created keep the policy they were evaluated with."
)


# ---------------------------------------------------------------------------
# What if - nothing saved
# ---------------------------------------------------------------------------

def simulate_what_if(
    run_id: str,
    policy_changes: list[str] = [],
    ceiling_price_changes: list[str] = [],
    volume_changes: list[str] = [],
    compliance_changes: list[str] = [],
) -> dict:
    """Re-run the real engine on this basket's real quotes with hypothetical
    changes, WITHOUT saving anything, and compare the outcome with the policy
    in force today.

    Use this for any "what if", "would", "suppose" or "what happens if"
    question. Never hand-compute the answer - report what this returns.

    Every change is a "KEY=VALUE" string. A bare number is the new value; a
    leading + or - is relative to the current value ("+2" adds 2 in the
    value's own unit, "-10%" takes off ten percent of the current value).

    Args:
        run_id: an evaluation run in the basket to simulate.
        policy_changes: rule thresholds, keys from get_current_policy, e.g.
            ["ceiling_materiality_pct=10"].
        ceiling_price_changes: a material's ceiling price in EUR per litre,
            keyed by CAS number from get_current_materials, e.g.
            ["7664-93-9=0.90"].
        volume_changes: a material's required volume in litres, keyed by CAS
            number, e.g. ["7664-93-9=40000"].
        compliance_changes: an existing checklist code's new tier - MANDATORY,
            ADVISORY or REMOVED - codes from get_current_compliance, e.g.
            ["SDS_LANGUAGE=MANDATORY"].
    """
    from ..evaluation import EvaluationError, simulate
    from ..reference_action import parse_changes

    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        if not run:
            return {"error": f"no evaluation run with id {run_id}"}

        changes, errors = parse_changes(
            session, policy_changes, ceiling_price_changes, volume_changes,
            compliance_changes)
        if not errors and changes.is_empty():
            errors = ["no changes were given"]
        if errors:
            return {"simulation": True, "saved": False, "errors": errors}

        try:
            baseline = simulate(session, run.comparison_id)
            simulated = simulate(session, run.comparison_id, changes)
        except EvaluationError as exc:
            return {"simulation": True, "saved": False, "errors": [str(exc)]}

        arguments = changes.as_arguments()
        log = _turn.get()
        if log is not None:
            log.last_simulation = arguments
        _record(tool="simulate_what_if", run_id=run_id, arguments=arguments)

        return {
            "simulation": True,
            "saved": False,
            "changes": changes.details,
            "compared_with": "this basket evaluated under the policy in force today",
            "policy_in_force_matches_this_run": _same_outcome(run.result or {}, baseline),
            "outcome": compare_outcomes(baseline, simulated),
            "to_apply_call_apply_changes_with": arguments,
        }


# ---------------------------------------------------------------------------
# Changes the buyer asked for - saved, and evaluated into a new run
# ---------------------------------------------------------------------------

def apply_changes(
    run_id: str,
    policy_changes: list[str] = [],
    ceiling_price_changes: list[str] = [],
    volume_changes: list[str] = [],
    compliance_changes: list[str] = [],
) -> dict:
    """SAVE changes to the policy in force, then re-evaluate this basket into
    a new evaluation run and compare it with run_id.

    Only call this when the buyer has asked for the change to be made, not
    when they are asking what would happen. Arguments are exactly those of
    simulate_what_if. Every change is validated first; if any one is invalid
    nothing at all is saved.

    Args:
        run_id: the evaluation run the buyer is looking at; its basket is
            re-evaluated.
        policy_changes: e.g. ["ceiling_materiality_pct=10"].
        ceiling_price_changes: e.g. ["7664-93-9=0.90"].
        volume_changes: e.g. ["7664-93-9=40000"].
        compliance_changes: e.g. ["SDS_LANGUAGE=MANDATORY"] or
            ["TSCA=REMOVED"].
    """
    from ..evaluation import EvaluationError, apply_changes_and_evaluate
    from ..reference_action import parse_changes

    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        if not run:
            return {"error": f"no evaluation run with id {run_id}"}

        changes, errors = parse_changes(
            session, policy_changes, ceiling_price_changes, volume_changes,
            compliance_changes)
        if not errors and changes.is_empty():
            errors = ["no changes were given"]
        if errors:
            return {"applied": False, "saved": False, "errors": errors}

        try:
            new_run = apply_changes_and_evaluate(session, run.comparison_id, changes)
        except EvaluationError as exc:
            session.rollback()
            return {"applied": False, "saved": False, "errors": [str(exc)]}

        _record(tool="apply_changes", run_id=run_id,
                arguments=changes.as_arguments(), changes=changes.details,
                new_run_id=new_run.run_id)
        return {
            "applied": True,
            "saved": True,
            "new_run_id": new_run.run_id,
            "previous_run_id": run_id,
            "changes": changes.details,
            "scope": SCOPE_NOTE,
            "outcome": compare_outcomes(run.result or {}, new_run.result or {}),
        }


def add_compliance_requirement(code: str, label: str, tier: str,
                               match_hint: str = "") -> dict:
    """SAVE a brand-new requirement to the compliance checklist.

    Only for a code that is not on the checklist yet - changing an existing
    code's tier, or removing it, is apply_changes with compliance_changes.
    Quotes already extracted were never checked against a new requirement, so
    this does not create a new evaluation run; see the note it returns.

    Args:
        code: short identifier, e.g. "ROHS". Normalized to upper case.
        label: what the requirement is, in words extraction can look for,
            e.g. "RoHS compliance declaration".
        tier: MANDATORY (Gate 1 exclusion) or ADVISORY (promotion rule).
        match_hint: optional comma-separated phrases a quote might use.
    """
    from ..models import ComplianceRequirement
    from ..reference_action import apply_compliance_updates, compliance_code

    normalized = compliance_code(code or "")
    with SessionLocal() as session:
        if normalized and session.get(ComplianceRequirement, normalized):
            return {"added": False, "saved": False, "errors": [
                f"{normalized} is already on the checklist; change its tier with "
                "apply_changes compliance_changes instead"]}
        result = apply_compliance_updates(session, [{
            "code": code or "", "label": label or "", "tier": tier or "",
            "match_hint": match_hint or None,
        }])

    if result["errors"]:
        return {"added": False, "saved": False,
                "errors": [e["error"] for e in result["errors"]]}

    created = result["created"][0]
    _record(tool="add_compliance_requirement", arguments=created)
    return {
        "added": True,
        "saved": True,
        "requirement": created,
        "scope": SCOPE_NOTE,
        "note": (
            "Quotes already extracted were never checked against this requirement, "
            "so every current quote counts it as a gap"
            + (" and would fail Gate 1" if created["tier"] == "MANDATORY" else "")
            + " until its document is reprocessed from the comparison page. No new "
            "evaluation run was created."
        ),
    }


def rerun_evaluation(run_id: str) -> dict:
    """Evaluate run_id's basket again under the policy in force now, into a
    new run, and compare it with run_id. Use when the buyer asks to re-run or
    refresh the evaluation - for example after policy was edited on the
    Policy in force screen.

    Args:
        run_id: the evaluation run whose basket is re-evaluated.
    """
    from ..evaluation import EvaluationError, run_evaluation

    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        if not run:
            return {"error": f"no evaluation run with id {run_id}"}
        try:
            new_run = run_evaluation(session, run.comparison_id)
        except EvaluationError as exc:
            return {"error": str(exc)}

        _record(tool="rerun_evaluation", run_id=run_id, new_run_id=new_run.run_id)
        return {
            "new_run_id": new_run.run_id,
            "previous_run_id": run_id,
            "outcome": compare_outcomes(run.result or {}, new_run.result or {}),
        }


READ_TOOLS = [
    get_run_summary,
    get_gate_results,
    get_promotion_detail,
    get_line_comparison,
    get_compliance,
    get_commercial_terms,
    get_renegotiation_candidates,
    get_allocation,
    get_historical_prices,
    get_data_quality,
]

POLICY_TOOLS = [
    get_current_policy,
    get_current_materials,
    get_current_compliance,
    simulate_what_if,
    apply_changes,
    add_compliance_requirement,
    rerun_evaluation,
]

# The one agent - the dashboard chat, the per-run ask endpoint and A2A all use
# this set. The memo writer gets READ_TOOLS only: drafting a document must
# never be able to change the policy it documents.
AGENT_TOOLS = READ_TOOLS + POLICY_TOOLS
