"""Tools the agent may call.

Every tool is a reader over a stored evaluation run. None of them computes
anything: the numbers were fixed when the run was written, and the agent's job
is to explain them, not to re-derive them. If the agent could recalculate, it
could disagree with the dashboard, and then neither would be trustworthy.
"""
from __future__ import annotations

from ..db import SessionLocal
from ..models import EvaluationRun


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


ALL_TOOLS = [
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
