"""Section 5.1 to 5.3 - the three exclusion gates.

Gates run in order and each records what it measured against what threshold,
so the trail can be shown rather than collapsed into a score.
"""
from __future__ import annotations

from decimal import Decimal

from .calc import ZERO, ConversionError, dstr, pct, quantity_to_litres
from .types import DemandLine, MaterialRef, Policy, QuoteInput

HUNDRED = Decimal("100")


def gate1_mandatory_compliance(quote: QuoteInput, policy: Policy) -> dict:
    """A supplier that does not state every mandatory requirement is excluded
    from ranking entirely, regardless of price."""
    met, gaps = [], []
    for code in policy.mandatory_requirements:
        claim = quote.compliance.get(code) or {}
        if claim.get("claimed"):
            met.append({
                "code": code,
                "evidence_text": claim.get("evidence_text"),
                "evidence_page": claim.get("evidence_page"),
            })
        else:
            gaps.append(code)

    return {
        "gate_no": 1,
        "gate_name": "Mandatory compliance",
        "passed": not gaps,
        "measured_value": len(gaps),
        "threshold_value": 0,
        "detail": {
            "met": met,
            "gaps": gaps,
            "explanation": (
                "All mandatory requirements are stated in the quote."
                if not gaps else
                "Not stated in the quote: " + ", ".join(gaps)
            ),
        },
    }


def gate2_moq_feasibility(
    quote: QuoteInput,
    demand: dict[str, DemandLine],
    materials: dict[str, MaterialRef],
    policy: Policy,
) -> dict:
    """Fails when any line's minimum order quantity would force the buyer to
    take materially more than they need. A waste check, not a preference."""
    worst = ZERO
    per_line = []

    for line in quote.lines:
        need = demand.get(line.cas_no)
        if not need or line.moq_qty is None:
            continue
        material = materials.get(line.cas_no)
        density = material.density_kg_per_l if material else None
        try:
            moq_l = quantity_to_litres(
                line.moq_qty, line.moq_uom or line.uom, density, policy)
        except ConversionError as exc:
            per_line.append({
                "cas_no": line.cas_no,
                "error": str(exc),
                "overbuy_pct": None,
            })
            continue

        overbuy = ZERO
        if moq_l > need.required_qty_l:
            overbuy = pct(moq_l - need.required_qty_l, need.required_qty_l) or ZERO
        worst = max(worst, overbuy)
        per_line.append({
            "cas_no": line.cas_no,
            "moq_qty_l": str(moq_l),
            "required_qty_l": str(need.required_qty_l),
            "overbuy_pct": str(overbuy),
            "exceeds": overbuy > policy.moq_overbuy_threshold_pct,
        })

    passed = worst <= policy.moq_overbuy_threshold_pct
    breaching = [p["cas_no"] for p in per_line if p.get("exceeds")]

    return {
        "gate_no": 2,
        "gate_name": "MOQ feasibility",
        "passed": passed,
        "measured_value": dstr(worst),
        "threshold_value": dstr(policy.moq_overbuy_threshold_pct),
        "detail": {
            "lines": per_line,
            "breaching_materials": breaching,
            "explanation": (
                f"Worst overbuy is {dstr(worst)}%, within the "
                f"{dstr(policy.moq_overbuy_threshold_pct)}% allowance."
                if passed else
                f"Minimum order quantity forces an overbuy of {dstr(worst)}% on "
                + ", ".join(breaching)
                + f", above the {dstr(policy.moq_overbuy_threshold_pct)}% allowance."
            ),
        },
    }


def gate3_ceiling_materiality(
    total_landed_cost: Decimal,
    ceiling_equivalent: Decimal,
    policy: Policy,
) -> dict:
    """Compares the supplier's basket total against what the basket would cost
    with every item at its category strategy ceiling."""
    variance = pct(total_landed_cost - ceiling_equivalent, ceiling_equivalent)
    passed = variance is not None and variance <= policy.ceiling_materiality_pct

    if variance is None:
        explanation = "No ceiling-equivalent total available for comparison."
    elif passed:
        explanation = (
            f"Basket total is {variance}% against the ceiling-equivalent "
            f"{ceiling_equivalent}, inside the "
            f"{dstr(policy.ceiling_materiality_pct)}% threshold."
        )
    else:
        explanation = (
            f"Basket total is +{variance}% over the ceiling-equivalent "
            f"{ceiling_equivalent}, above the "
            f"{dstr(policy.ceiling_materiality_pct)}% threshold. Drops out of primary "
            f"ranking, back-up only."
        )

    return {
        "gate_no": 3,
        "gate_name": "Ceiling materiality",
        "passed": bool(passed),
        "measured_value": str(variance) if variance is not None else None,
        "threshold_value": dstr(policy.ceiling_materiality_pct),
        "detail": {
            "total_landed_cost_eur": dstr(total_landed_cost),
            "ceiling_equivalent_total_eur": dstr(ceiling_equivalent),
            "variance_pct": str(variance) if variance is not None else None,
            "explanation": explanation,
        },
    }
