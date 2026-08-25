"""Section 5.4 to 5.6 - base ranking, the promotion rule and award allocation.

The promotion rule may only move a supplier above a cheaper one when all four
conditions hold independently. Where this code and the rule disagree, the rule
wins - which is why each condition is evaluated and stored separately rather
than combined into a single boolean expression.
"""
from __future__ import annotations

from decimal import Decimal

from .calc import ZERO, dstr, pct
from .types import Policy, QuoteInput

HUNDRED = Decimal("100")


def advisory_gaps(quote: QuoteInput, policy: Policy) -> list[str]:
    """Advisory checklist items the quote does not address.

    A requirement that is simply not mentioned is a gap - never assumed met.
    """
    return [
        code for code in policy.advisory_requirements
        if not (quote.compliance.get(code) or {}).get("claimed")
    ]


def lead_time_midpoint(quote: QuoteInput) -> Decimal | None:
    lo, hi = quote.lead_time_min_weeks, quote.lead_time_max_weeks
    if lo is None and hi is None:
        return None
    if lo is None:
        return hi
    if hi is None:
        return lo
    return (lo + hi) / 2


def base_rank(eligible: list[dict]) -> list[dict]:
    """Ascending total landed cost among suppliers that cleared every gate."""
    ordered = sorted(eligible, key=lambda s: s["total_landed_cost_eur"])
    for position, supplier in enumerate(ordered, start=1):
        supplier["base_rank"] = position
    return ordered


def evaluate_promotion(
    candidate: dict,
    cheaper: dict,
    quotes: dict[str, QuoteInput],
    policy: Policy,
) -> dict:
    """All four conditions of the promotion rule, each independently checkable."""
    candidate_quote = quotes[candidate["supplier_id"]]
    cheaper_quote = quotes[cheaper["supplier_id"]]

    gap = pct(
        candidate["total_landed_cost_eur"] - cheaper["total_landed_cost_eur"],
        cheaper["total_landed_cost_eur"],
    )
    cost_ok = gap is not None and gap <= policy.promotion_band_pct

    candidate_gaps = advisory_gaps(candidate_quote, policy)
    cheaper_gaps = advisory_gaps(cheaper_quote, policy)
    # The cheaper supplier must carry gaps that the candidate does not.
    exclusive_gaps = [g for g in cheaper_gaps if g not in candidate_gaps]
    compliance_ok = bool(exclusive_gaps)

    cand_days = candidate_quote.payment_terms_net_days
    cheap_days = cheaper_quote.payment_terms_net_days
    payment_ok = (
        cand_days is not None and cheap_days is not None and cand_days >= cheap_days
    )

    cand_lead = lead_time_midpoint(candidate_quote)
    cheap_lead = lead_time_midpoint(cheaper_quote)
    lead_ok = (
        cand_lead is not None and cheap_lead is not None and cand_lead <= cheap_lead
    )

    promoted = cost_ok and compliance_ok and payment_ok and lead_ok

    return {
        "candidate_supplier_id": candidate["supplier_id"],
        "cheaper_supplier_id": cheaper["supplier_id"],
        "cost_gap_pct": str(gap) if gap is not None else None,
        "cost_condition_met": cost_ok,
        "compliance_condition_met": compliance_ok,
        "compliance_gaps_of_cheaper": exclusive_gaps,
        "payment_condition_met": payment_ok,
        "lead_time_condition_met": lead_ok,
        "promoted": promoted,
        "detail": {
            "cost": (
                f"Cost gap {dstr(gap)}% against the {dstr(policy.promotion_band_pct)}% band"
                if gap is not None else "Cost gap could not be computed"
            ),
            "compliance": (
                f"{cheaper['supplier_id']} has gaps the candidate does not: "
                + ", ".join(exclusive_gaps)
                if exclusive_gaps else
                f"{cheaper['supplier_id']} has no advisory gap the candidate avoids"
            ),
            "payment_terms": (
                f"Candidate Net {cand_days} against Net {cheap_days}"
                if cand_days is not None and cheap_days is not None
                else "Payment terms missing on one side"
            ),
            "lead_time": (
                f"Candidate midpoint {dstr(cand_lead)} weeks against {dstr(cheap_lead)} weeks"
                if cand_lead is not None and cheap_lead is not None
                else "Lead time missing on one side"
            ),
        },
    }


def apply_promotion(
    ranked: list[dict], quotes: dict[str, QuoteInput], policy: Policy
) -> tuple[list[dict], list[dict]]:
    """Each supplier is tested once against the one immediately above it.

    No cascading: a promoted supplier is not then re-tested against the next
    one up, which keeps the trail readable and the outcome order-independent.
    """
    evaluations: list[dict] = []
    order = list(ranked)

    for index in range(1, len(order)):
        candidate, cheaper = order[index], order[index - 1]
        evaluation = evaluate_promotion(candidate, cheaper, quotes, policy)
        evaluations.append(evaluation)
        if evaluation["promoted"]:
            order[index - 1], order[index] = candidate, cheaper

    for position, supplier in enumerate(order, start=1):
        supplier["final_rank"] = position

    return order, evaluations


def allocate_award(ranked: list[dict], policy: Policy) -> list[dict]:
    """Split basket spend across the awarded suppliers and test it against the
    concentration threshold.

    The source document does not state a split, so the primary share is taken
    from policy and labelled as an assumption.
    """
    awarded = [s for s in ranked if s.get("final_rank") in (1, 2)]
    if not awarded:
        return []

    if len(awarded) == 1:
        shares = [Decimal("100")]
    else:
        primary = policy.primary_allocation_pct
        shares = [primary, Decimal("100") - primary]

    allocations = []
    for supplier, share in zip(awarded, shares):
        spend = (supplier["total_landed_cost_eur"] * share / HUNDRED).quantize(
            Decimal("0.01"))
        allocations.append({
            "supplier_id": supplier["supplier_id"],
            "allocation_pct": dstr(share),
            "allocated_spend_eur": str(spend),
            "exceeds_concentration_threshold": share > policy.max_vendor_share_pct,
        })
    return allocations


def award_status(supplier: dict) -> tuple[str, str]:
    """Award label and the headline reason shown in the ranking table."""
    if not supplier.get("eligible"):
        failed = supplier.get("failed_gate")
        if failed == 3:
            return "NOT_RECOMMENDED", (
                "Failed Gate 3: landed cost above the ceiling-equivalent "
                "materiality threshold. Back-up only."
            )
        return "EXCLUDED", f"Excluded at Gate {failed}."

    rank = supplier.get("final_rank")
    if rank == 1:
        if supplier.get("promoted_over"):
            return "PRIMARY", (
                f"Promoted over lower-cost {supplier['promoted_over']}: passes all "
                "gates, fewer compliance gaps, better payment terms and faster "
                "lead time, with the cost gap inside the promotion band."
            )
        return "PRIMARY", "Lowest total landed cost among suppliers passing all gates."
    if rank == 2:
        return "SECONDARY", (
            "Retained as second source under the dual-sourcing policy."
        )
    return "NOT_RECOMMENDED", "Ranked below the awarded suppliers."
