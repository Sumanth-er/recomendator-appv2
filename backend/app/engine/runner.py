"""Engine entry point.

evaluate() takes the quotes plus reference data and returns the complete result
for one run: per-line derivations, supplier totals, the gate trail, the
promotion decision, allocation and renegotiation candidates.

Nothing in this module calls a model, a database or a cloud service.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .calc import (
    ONE, ZERO, ConversionError, applicable_discount, apply_freight,
    ceiling_equivalent_total, check_line_total, freight_for, money, pct,
    price_to_eur_per_litre, q, dstr,
)
from .gates import gate1_mandatory_compliance, gate2_moq_feasibility, gate3_ceiling_materiality
from .ranking import (
    advisory_gaps, allocate_award, apply_promotion, award_status, base_rank,
    lead_time_midpoint,
)
from .types import (
    BenchmarkRef, DemandLine, HistoricalRef, MaterialRef, Policy, QuoteInput,
)

HUNDRED = Decimal("100")


def evaluate(
    quotes: list[QuoteInput],
    materials: dict[str, MaterialRef],
    demand: dict[str, DemandLine],
    benchmarks: dict[str, BenchmarkRef],
    policy: Policy,
    historical: dict[str, HistoricalRef] | None = None,
    incumbent_spend: dict[str, Decimal] | None = None,
    engine_version: str = "1.0.0",
) -> dict:
    historical = historical or {}
    incumbent_spend = incumbent_spend or {}
    incumbent_total = sum(incumbent_spend.values()) if incumbent_spend else ZERO
    ceilings = {cas: b.ceiling_price_eur_l for cas, b in benchmarks.items()}
    ceiling_equiv = ceiling_equivalent_total(demand, ceilings)

    line_rows: list[dict] = []
    supplier_rows: list[dict] = []
    warnings: list[str] = []

    # ---- Section 3: normalize, add freight, extend by required volume ----
    for quote in quotes:
        freight_pct, freight_basis, freight_matched = freight_for(
            quote.incoterm, policy)
        if not freight_matched:
            warnings.append(
                f"{quote.supplier_name}: no freight policy matches Incoterm "
                f"{quote.incoterm or '(none extracted)'}, so no uplift was "
                "applied. Their landed cost is understated against suppliers "
                "that did match."
            )
        matched_cas: set[str] = set()
        goods_subtotal = ZERO
        landed_subtotal = ZERO

        for line in quote.lines:
            if line.cas_no not in demand:
                warnings.append(
                    f"{quote.supplier_name} line for CAS {line.cas_no} is not in the "
                    "demand basket and was excluded from the totals"
                )
                continue

            material = materials.get(line.cas_no)
            density = material.density_kg_per_l if material else None
            required = demand[line.cas_no].required_qty_l

            try:
                price_eur_l, factor, density_used, fx = price_to_eur_per_litre(
                    line.unit_price, line.currency, line.uom, density, policy)
            except ConversionError as exc:
                warnings.append(
                    f"{quote.supplier_name} line for CAS {line.cas_no} could not be "
                    f"normalized: {exc}"
                )
                continue

            matched_cas.add(line.cas_no)
            # Rounding is presentation only. The spec's own numbers require
            # this: its section 4.1 grid at two decimals extends to 176,420 for
            # the kg-quoted supplier, while section 3.3 states 176,400. The
            # grid is rounded for display; the totals come from full precision.
            price_display = q(price_eur_l, policy.price_rounding_dp)
            landed_full = price_eur_l * (ONE + freight_pct / HUNDRED)
            landed = q(landed_full, policy.price_rounding_dp)
            extended = money(landed_full * required)
            goods_subtotal += price_eur_l * required
            landed_subtotal += landed_full * required

            ceiling = ceilings.get(line.cas_no)
            hist = historical.get(line.cas_no)
            recomputed, delta, dq_flag, dq_note = check_line_total(line, policy)

            line_rows.append({
                "quote_line_id": line.quote_line_id,
                "supplier_id": quote.supplier_id,
                "supplier_name": quote.supplier_name,
                "cas_no": line.cas_no,
                "material_name": material.name if material else line.cas_no,
                "supplier_description": line.supplier_description,

                "quoted_unit_price": dstr(line.unit_price),
                "quoted_currency": line.currency,
                "quoted_uom": line.uom,
                "uom_factor_applied": dstr(factor),
                "density_applied": dstr(density_used),
                "fx_rate_applied": dstr(fx),
                "price_per_l_eur": str(price_display),
                "freight_adj_pct": dstr(freight_pct),
                "freight_basis": freight_basis,
                "landed_price_per_l_eur": str(landed),
                "required_qty_l": dstr(required),
                "extended_landed_cost_eur": str(extended),

                "ceiling_price_eur_l": dstr(ceiling),
                "ceiling_variance_pct": (
                    str(pct(landed - ceiling, ceiling)) if ceiling else None),
                "above_ceiling": bool(ceiling is not None and landed > ceiling),
                "historical_avg_eur_l": (
                    dstr(hist.avg_price_eur_l)
                    if hist and hist.avg_price_eur_l else None),
                "historical_variance_pct": (
                    str(pct(landed - hist.avg_price_eur_l, hist.avg_price_eur_l))
                    if hist and hist.avg_price_eur_l else None),
                "historical_last_invoiced_eur_l": (
                    dstr(hist.last_invoiced_price_eur_l) if hist else None),
                "historical_min_eur_l": dstr(hist.min_price_eur_l) if hist else None,
                "historical_max_eur_l": dstr(hist.max_price_eur_l) if hist else None,
                "historical_po_line_count": hist.po_line_count if hist else None,

                # A currency figure, shown at two decimals whatever scale the
                # column carried it in.
                "line_total_stated": (
                    str(money(line.line_total_stated))
                    if line.line_total_stated is not None else None),
                "line_total_recomputed": str(recomputed) if recomputed else None,
                "line_total_delta": str(delta) if delta is not None else None,
                "data_quality_flag": dq_flag,
                "data_quality_note": dq_note,
                "is_cheapest_for_material": False,
            })

        # ---- Section 3.3: basket total ----
        discount_pct, discount_text, discount_met = applicable_discount(
            quote, matched_cas, demand)

        # Section 3.3: the discount comes off the landed sum. Both freight and
        # discount are percentages of the whole basket, so the order they are
        # applied in cannot change the answer.
        discount_amount_full = landed_subtotal * discount_pct / HUNDRED
        total = money(landed_subtotal - discount_amount_full)
        discount_amount = money(discount_amount_full)
        freight_amount = money(landed_subtotal - goods_subtotal)
        goods_subtotal = money(goods_subtotal)
        landed_subtotal = money(landed_subtotal)

        missing = sorted(set(demand) - matched_cas)
        if missing:
            warnings.append(
                f"{quote.supplier_name} did not quote "
                f"{len(missing)} of {len(demand)} required materials; totals cover "
                "only the items quoted"
            )

        supplier_rows.append({
            "supplier_id": quote.supplier_id,
            "supplier_name": quote.supplier_name,
            "quote_id": quote.quote_id,
            "incoterm": quote.incoterm,
            "currency": quote.currency,
            "goods_subtotal_eur": goods_subtotal,
            "freight_adj_pct": dstr(freight_pct),
            "freight_basis": freight_basis,
            "freight_policy_matched": freight_matched,
            "freight_amount_eur": freight_amount,
            "landed_subtotal_eur": landed_subtotal,
            "discount_pct_applied": dstr(discount_pct),
            "discount_condition_met": discount_met,
            "discount_condition_text": discount_text,
            "discount_amount_eur": discount_amount,
            "total_landed_cost_eur": total,
            "payment_terms_net_days": quote.payment_terms_net_days,
            "lead_time_min_weeks": dstr(quote.lead_time_min_weeks),
            "lead_time_max_weeks": dstr(quote.lead_time_max_weeks),
            "lead_time_midpoint_weeks": dstr(lead_time_midpoint(quote)),
            "missing_materials": missing,
            "advisory_gaps": advisory_gaps(quote, policy),
            "is_approved_supplier": quote.is_approved,
            # Spec 5.5: what this supplier already holds, measured from the PO
            # history rather than assumed.
            "is_incumbent": quote.supplier_id in incumbent_spend,
            "historical_spend_eur": (
                str(money(incumbent_spend[quote.supplier_id]))
                if quote.supplier_id in incumbent_spend else None),
            "historical_share_pct": (
                str(pct(incumbent_spend[quote.supplier_id], incumbent_total))
                if quote.supplier_id in incumbent_spend and incumbent_total else None),
            # Comparison 6: MOQ terms and discount structure belong beside the
            # other commercial terms, not only on the extraction screen.
            "moq_terms": [
                {"cas_no": line.cas_no,
                 "text": line.moq_text,
                 "qty": dstr(line.moq_qty),
                 "uom": line.moq_uom}
                for line in quote.lines
                if line.moq_text or line.moq_qty is not None
            ],
            "discount_structure": [
                {"discount_pct": dstr(d.discount_pct),
                 "condition_type": d.condition_type,
                 "condition_text": d.condition_text}
                for d in quote.discounts
            ],
        })

    # ---- Section 4.1: cheapest supplier per material ----
    for cas in demand:
        candidates = [r for r in line_rows if r["cas_no"] == cas]
        if candidates:
            best = min(candidates, key=lambda r: Decimal(r["landed_price_per_l_eur"]))
            best["is_cheapest_for_material"] = True

    # ---- Section 5.1 to 5.3: the gates ----
    quotes_by_supplier = {q.supplier_id: q for q in quotes}
    gates: dict[str, list[dict]] = {}

    for row in supplier_rows:
        quote = quotes_by_supplier[row["supplier_id"]]
        g1 = gate1_mandatory_compliance(quote, policy)
        g2 = gate2_moq_feasibility(quote, demand, materials, policy)
        g3 = gate3_ceiling_materiality(
            row["total_landed_cost_eur"], ceiling_equiv, policy)
        gates[row["supplier_id"]] = [g1, g2, g3]

        failed = next((g["gate_no"] for g in (g1, g2, g3) if not g["passed"]), None)
        row["eligible"] = failed is None
        row["failed_gate"] = failed
        row["ceiling_equivalent_variance_pct"] = g3["measured_value"]

    # ---- Section 5.4 to 5.6: rank, promote, allocate ----
    eligible = [r for r in supplier_rows if r["eligible"]]
    ranked = base_rank(eligible)
    ranked, promotions = apply_promotion(ranked, quotes_by_supplier, policy)

    for promo in promotions:
        if promo["promoted"]:
            for row in ranked:
                if row["supplier_id"] == promo["candidate_supplier_id"]:
                    row["promoted_over"] = promo["cheaper_supplier_id"]

    for row in supplier_rows:
        status, reason = award_status(row)
        row["award_status"] = status
        row["primary_reason"] = reason

    allocation = allocate_award(ranked, policy)
    # Show the proposed share against what that supplier already holds, so the
    # dual-sourcing conversation is about a measured shift, not an assumption.
    shares = {r["supplier_id"]: r.get("historical_share_pct") for r in supplier_rows}
    for entry in allocation:
        entry["historical_share_pct"] = shares.get(entry["supplier_id"])

    # ---- Renegotiation candidates: any line above its ceiling ----
    renegotiation = []
    for row in line_rows:
        if not row["above_ceiling"]:
            continue
        landed = Decimal(row["landed_price_per_l_eur"])
        ceiling = Decimal(row["ceiling_price_eur_l"])
        required = Decimal(row["required_qty_l"])
        renegotiation.append({
            "supplier_id": row["supplier_id"],
            "supplier_name": row["supplier_name"],
            "cas_no": row["cas_no"],
            "material_name": row["material_name"],
            "landed_price_eur_l": row["landed_price_per_l_eur"],
            "ceiling_price_eur_l": row["ceiling_price_eur_l"],
            "gap_pct": row["ceiling_variance_pct"],
            "annual_impact_eur": str(money((landed - ceiling) * required)),
        })
    renegotiation.sort(key=lambda r: Decimal(r["annual_impact_eur"]), reverse=True)

    # ---- Historical context, shown beside the ceiling comparison (spec 4.2) ----
    historical_context = {
        "materials": [
            {
                "cas_no": cas,
                "avg_price_eur_l": dstr(ref.avg_price_eur_l),
                "min_price_eur_l": dstr(ref.min_price_eur_l),
                "max_price_eur_l": dstr(ref.max_price_eur_l),
                "last_invoiced_price_eur_l": dstr(ref.last_invoiced_price_eur_l),
                "po_line_count": ref.po_line_count,
                "period_from": ref.period_from,
                "period_to": ref.period_to,
            }
            for cas, ref in sorted(historical.items())
        ],
        "incumbent_spend_eur": str(money(incumbent_total)) if incumbent_total else None,
        "incumbent_vendors": [
            {
                "supplier_id": key,
                "spend_eur": str(money(value)),
                "share_pct": str(pct(value, incumbent_total)) if incumbent_total else None,
                "is_quoting": any(r["supplier_id"] == key for r in supplier_rows),
                "exceeds_threshold_today": bool(
                    incumbent_total
                    and pct(value, incumbent_total) > policy.max_vendor_share_pct),
            }
            for key, value in sorted(
                incumbent_spend.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "concentration_threshold_pct": dstr(policy.max_vendor_share_pct),
    }

    # Spec 5.5: whether the award supports the policy is only answerable against
    # what concentration exists today, so a standing breach is reported too.
    for vendor in historical_context["incumbent_vendors"]:
        if vendor["exceeds_threshold_today"]:
            proposed = next((a["allocation_pct"] for a in allocation
                             if a["supplier_id"] == vendor["supplier_id"]), None)
            warnings.append(
                f"{vendor['supplier_id']} already holds {vendor['share_pct']}% of "
                f"historical spend, above the {dstr(policy.max_vendor_share_pct)}% "
                "concentration threshold"
                + (f"; the proposed award moves it to {proposed}%"
                   if proposed else " and is not among the awarded suppliers")
            )

    # ---- Comparison 7: checklist matrix, met or gap, per supplier ----
    compliance_matrix = []
    for code in list(policy.mandatory_requirements) + list(policy.advisory_requirements):
        tier = "MANDATORY" if code in policy.mandatory_requirements else "ADVISORY"
        row = {
            "code": code,
            "label": (policy.requirement_labels or {}).get(code, code),
            "tier": tier,
            "suppliers": {},
        }
        for quote in quotes:
            claim = quote.compliance.get(code) or {}
            row["suppliers"][quote.supplier_id] = {
                "claimed": bool(claim.get("claimed")),
                "evidence_text": claim.get("evidence_text"),
                "evidence_page": claim.get("evidence_page"),
            }
        compliance_matrix.append(row)

    # ---- Comparison 9: every line that failed its consistency check ----
    data_quality = [
        {
            "supplier_id": row["supplier_id"],
            "supplier_name": row["supplier_name"],
            "cas_no": row["cas_no"],
            "material_name": row["material_name"],
            "flag": row["data_quality_flag"],
            "note": row["data_quality_note"],
            "line_total_stated": row["line_total_stated"],
            "line_total_recomputed": row["line_total_recomputed"],
            "line_total_delta": row["line_total_delta"],
        }
        for row in line_rows if row["data_quality_flag"] != "OK"
    ]

    # ---- Spec 2.3: quoting suppliers must be on the approved list ----
    for row in supplier_rows:
        if not row.get("is_approved_supplier"):
            warnings.append(
                f"{row['supplier_name']} is not on the approved supplier list for "
                "this category"
            )

    # ---- Headline figures ----
    totals = [r["total_landed_cost_eur"] for r in supplier_rows]
    cheapest = min(totals) if totals else ZERO
    dearest = max(totals) if totals else ZERO
    primary = next((r for r in ranked if r.get("final_rank") == 1), None)

    kpis = {
        "supplier_count": len(supplier_rows),
        "material_count": len(demand),
        "ceiling_equivalent_total_eur": str(ceiling_equiv),
        "cheapest_total_eur": str(cheapest),
        "most_expensive_total_eur": str(dearest),
        "spread_eur": str(money(dearest - cheapest)),
        # Section 4.3 reads the spread as a share of total basket value, i.e.
        # against the most expensive basket, not the cheapest.
        "spread_pct": str(pct(dearest - cheapest, dearest)) if dearest else None,
        "recommended_supplier": primary["supplier_name"] if primary else None,
        "recommended_total_eur": (
            str(primary["total_landed_cost_eur"]) if primary else None),
        "savings_vs_most_expensive_eur": (
            str(money(dearest - primary["total_landed_cost_eur"])) if primary else None),
        "renegotiation_count": len(renegotiation),
        "data_quality_issues": len(data_quality),
        "unapproved_suppliers": [
            r["supplier_name"] for r in supplier_rows
            if not r.get("is_approved_supplier")],
    }

    for row in supplier_rows:
        for key in ("goods_subtotal_eur", "freight_amount_eur", "landed_subtotal_eur",
                    "discount_amount_eur", "total_landed_cost_eur"):
            row[key] = str(row[key])

    return {
        "engine_version": engine_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": kpis,
        "ceiling_equivalent_total_eur": str(ceiling_equiv),
        "suppliers": sorted(
            supplier_rows,
            key=lambda r: (r.get("final_rank") or 99, r["supplier_name"])),
        "lines": line_rows,
        "gates": gates,
        "promotions": promotions,
        "allocation": allocation,
        "renegotiation": renegotiation,
        "compliance_matrix": compliance_matrix,
        "historical_context": historical_context,
        "data_quality": data_quality,
        "warnings": warnings,
    }
