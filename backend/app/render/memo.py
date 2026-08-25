"""Deterministic approval package.

The agent writes the prose version. This renders the same memo straight from
the stored run, with no model involved - used as the fallback, and as the
reference the agent's version can be checked against.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from ..db import SessionLocal
from ..models import EvaluationRun


def eur(value) -> str:
    """Money with thousands separators - this document goes to management."""
    if value in (None, ""):
        return "-"
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def render_memo(run_id: str) -> str:
    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        if not run:
            return "That run could not be found."
        result = run.result or {}
        policy = run.policy_snapshot or {}

    kpis = result.get("kpis", {})
    suppliers = result.get("suppliers", [])
    gates = result.get("gates", {})
    primary = next((s for s in suppliers if s.get("final_rank") == 1), None)
    secondary = next((s for s in suppliers if s.get("final_rank") == 2), None)

    out: list[str] = ["# Approval package summary", ""]
    out.append(f"Semiconductor grade wet chemicals, {kpis.get('material_count')} "
               f"materials, {kpis.get('supplier_count')} suppliers, "
               f"delivery to a single plant.")
    out.append("")

    # --- Recommendation ---
    out += ["## Recommendation", ""]
    if primary:
        out.append(
            f"**{primary['supplier_name']}** is recommended as primary award at "
            f"EUR {eur(primary['total_landed_cost_eur'])} total landed cost. "
            f"{primary.get('primary_reason')}"
        )
        if secondary:
            out.append("")
            out.append(
                f"**{secondary['supplier_name']}** is retained as second source at "
                f"EUR {eur(secondary['total_landed_cost_eur'])}. "
                f"{secondary.get('primary_reason')}"
            )
    else:
        out.append("No supplier cleared every gate, so no award is recommended.")
    out.append("")

    # --- Commercial summary ---
    out += ["## Commercial summary", "",
            "| Supplier | Total landed cost (EUR) | Incoterm | Payment terms | "
            "Lead time (weeks) | Status |",
            "|---|---:|---|---|---|---|"]
    for supplier in suppliers:
        lead = "-"
        if supplier.get("lead_time_min_weeks") and supplier.get("lead_time_max_weeks"):
            lead = (f"{supplier['lead_time_min_weeks']}-"
                    f"{supplier['lead_time_max_weeks']}")
        payment = (f"Net {supplier['payment_terms_net_days']}"
                   if supplier.get("payment_terms_net_days") else "-")
        out.append(
            f"| {supplier['supplier_name']} | {eur(supplier['total_landed_cost_eur'])} "
            f"| {supplier.get('incoterm') or '-'} | {payment} | {lead} "
            f"| {supplier.get('award_status')} |"
        )
    out.append("")
    out.append(
        f"Spread between cheapest and most expensive: EUR {eur(kpis.get('spread_eur'))} "
        f"({kpis.get('spread_pct')}% of basket value). Ceiling-equivalent basket "
        f"total: EUR {eur(result.get('ceiling_equivalent_total_eur'))}."
    )
    out.append("")

    # --- Ranking trail ---
    out += ["## How the ranking was reached", ""]
    for supplier in suppliers:
        out.append(f"**{supplier['supplier_name']}**")
        out.append("")
        for gate in gates.get(supplier["supplier_id"], []):
            verdict = "pass" if gate["passed"] else "fail"
            out.append(
                f"- Gate {gate['gate_no']}, {gate['gate_name']}: **{verdict}**. "
                f"{gate['detail'].get('explanation', '')}"
            )
        if supplier.get("base_rank"):
            out.append(f"- Base rank by cost: {supplier['base_rank']}")
        if supplier.get("final_rank"):
            out.append(f"- Final rank: {supplier['final_rank']}")
        out.append("")

    for promo in result.get("promotions", []):
        out.append(
            f"**Promotion rule, {promo['candidate_supplier_id']} against "
            f"{promo['cheaper_supplier_id']}**"
        )
        out.append("")
        detail = promo.get("detail", {})
        for label, key, note in (
            ("Cost", "cost_condition_met", detail.get("cost")),
            ("Compliance", "compliance_condition_met", detail.get("compliance")),
            ("Payment terms", "payment_condition_met", detail.get("payment_terms")),
            ("Lead time", "lead_time_condition_met", detail.get("lead_time")),
        ):
            mark = "met" if promo.get(key) else "not met"
            out.append(f"- {label}: **{mark}**. {note}")
        outcome = ("promoted above the cheaper supplier"
                   if promo["promoted"] else "not promoted")
        out.append(f"- Outcome: {outcome}, all four conditions required.")
        out.append("")

    # --- Compliance ---
    out += ["## Compliance position", ""]
    for supplier in suppliers:
        gate1 = (gates.get(supplier["supplier_id"]) or [{}])[0]
        mandatory = "all stated" if gate1.get("passed") else (
            "gaps: " + ", ".join(gate1.get("detail", {}).get("gaps", [])))
        advisory = supplier.get("advisory_gaps") or []
        out.append(
            f"- **{supplier['supplier_name']}** - mandatory: {mandatory}. "
            f"Advisory gaps: {', '.join(advisory) if advisory else 'none'}."
        )
    out.append("")

    # --- Historical reference (spec 4.2) ---
    historical_lines = [l for l in result.get("lines", [])
                        if l.get("historical_variance_pct") is not None]
    if historical_lines:
        out += ["## Against historical prices", "",
                "| Supplier | Material | Landed (EUR/L) | Historical avg | Range | Variance |",
                "|---|---|---:|---:|---:|---:|"]
        for row in sorted(historical_lines,
                          key=lambda r: (r["material_name"], r["supplier_name"])):
            low, high = row.get("historical_min_eur_l"), row.get("historical_max_eur_l")
            span = f"{low}-{high}" if low and high else "-"
            out.append(
                f"| {row['supplier_name']} | {row['material_name']} "
                f"| {row['landed_price_per_l_eur']} | {row['historical_avg_eur_l']} "
                f"| {span} | {row['historical_variance_pct']}% |"
            )
        out.append("")

    context = result.get("historical_context") or {}
    vendors = context.get("incumbent_vendors") or []
    if vendors:
        allocation = {a["supplier_id"]: a for a in result.get("allocation", [])}
        names = {s["supplier_id"]: s["supplier_name"]
                 for s in result.get("suppliers", [])}
        out += ["## Dual sourcing", "",
                f"Concentration threshold: {context.get('concentration_threshold_pct')}%.",
                "",
                "| Supplier | Share today | Proposed share |", "|---|---:|---:|"]
        for vendor in vendors:
            proposed = allocation.get(vendor["supplier_id"], {}).get("allocation_pct")
            label = names.get(vendor["supplier_id"], vendor["supplier_id"])
            out.append(
                f"| {label} | {vendor['share_pct']}% "
                f"| {proposed + '%' if proposed else 'not awarded'} |"
            )
        out.append("")

    # --- Negotiation ---
    out += ["## Negotiation priorities", ""]
    candidates = result.get("renegotiation", [])
    if candidates:
        out += ["| Supplier | Material | Landed (EUR/L) | Ceiling (EUR/L) | "
                "Gap | Annual impact (EUR) |", "|---|---|---:|---:|---:|---:|"]
        for row in candidates:
            out.append(
                f"| {row['supplier_name']} | {row['material_name']} "
                f"| {row['landed_price_eur_l']} | {row['ceiling_price_eur_l']} "
                f"| {row['gap_pct']}% | {eur(row['annual_impact_eur'])} |"
            )
    else:
        out.append("No line item is priced above its category strategy ceiling.")
    out.append("")

    # --- Allocation ---
    allocation = result.get("allocation", [])
    if allocation:
        out += ["## Award allocation", ""]
        for row in allocation:
            breach = (" - **exceeds the concentration threshold**"
                      if row["exceeds_concentration_threshold"] else "")
            out.append(
                f"- {row['supplier_id']}: {row['allocation_pct']}% of basket spend, "
                f"EUR {eur(row['allocated_spend_eur'])}{breach}"
            )
        out.append("")

    # --- Assumptions ---
    out += ["## Assumptions and caveats", ""]
    fx = policy.get("fx", {})
    if fx:
        out.append("- FX rates applied: "
                   + ", ".join(f"1 {k} = {v} EUR" for k, v in fx.items()))
    freight = policy.get("freight_by_incoterm", {})
    for incoterm, meta in freight.items():
        if meta.get("pct") not in (None, "0"):
            out.append(f"- {incoterm} freight adjustment {meta['pct']}%, "
                       f"estimated: {meta['basis']}")
    out.append(f"- Award split assumes {policy.get('primary_allocation_pct')}% to the "
               f"primary supplier. The category strategy does not state a split; "
               f"this is an assumption.")
    out.append(f"- Thresholds: Gate 2 MOQ overbuy "
               f"{policy.get('moq_overbuy_threshold_pct')}%, Gate 3 ceiling "
               f"{policy.get('ceiling_materiality_pct')}%, promotion band "
               f"{policy.get('promotion_band_pct')}%.")
    issues = kpis.get("data_quality_issues")
    if issues:
        out.append(f"- {issues} line items have a data quality flag; see the "
                   f"dashboard for details.")
    for warning in result.get("warnings", []):
        out.append(f"- {warning}")
    out.append("")
    out.append(f"_Generated by engine {result.get('engine_version')} on "
               f"{result.get('generated_at')}. Every figure above is either taken "
               f"from a supplier quote or derived by the engine as labelled._")

    return "\n".join(out)
