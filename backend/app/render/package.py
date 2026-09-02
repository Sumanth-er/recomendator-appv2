"""The approval package, as structure rather than prose.

build_package() turns a stored run into the ten sections of the sourcing
approval package, as plain dictionaries. Two formatters render it: markdown for
the browser and the agent's fallback, and Word for the file a buyer sends on.
Both read this one structure, so the document on screen and the document that
downloads cannot drift apart.

Nothing here computes a price. Every figure is a value the engine already
stored on the run; what this module does is select, group and compare values
that are already fixed, which is why the same run always produces the same
package.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from ..db import SessionLocal
from ..models import (
    CategoryStrategy, Comparison, Demand, EvaluationRun, SourceDocument,
)

# Requirements kept in the data but not shown to a reader. TSCA is collected
# because the quotations state it, and it stays on quote.compliance and in the
# stored run - it is just not a decision input for this category, so putting it
# in front of an approver only invites a question with no answer. Mirrors
# HIDDEN_COMPLIANCE in frontend/app.js; change both together.
HIDDEN_REQUIREMENTS = {"TSCA"}


def visible(codes) -> list[str]:
    return [c for c in (codes or []) if c not in HIDDEN_REQUIREMENTS]


# --- block helpers ---------------------------------------------------------
# A section is a list of blocks. Keeping them as data rather than strings is
# what lets the Word writer make a real table where markdown makes pipes.


def para(text: str) -> dict:
    return {"type": "para", "text": text}


def bullets(items: list[str]) -> dict:
    return {"type": "bullets", "items": [i for i in items if i]}


def table(header: list[str], rows: list[list[str]], numeric: list[int] | None = None) -> dict:
    return {"type": "table", "header": header, "rows": rows,
            "numeric": numeric or []}


def note(text: str) -> dict:
    return {"type": "note", "text": text}


def fields(rows: list[tuple[str, str]]) -> dict:
    """Two-column label/value table - the header block and the approval block."""
    return {"type": "fields", "rows": rows}


def signature() -> dict:
    return {"type": "signature"}


# --- formatting ------------------------------------------------------------

def eur(value) -> str:
    """Money with thousands separators - this document goes to management."""
    if value in (None, ""):
        return "-"
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def pct(value) -> str:
    """A percentage without the column scale trailing behind it.

    policy_snapshot stores these as Decimal strings, so a 60% threshold comes
    back as "60.000000" and reads as false precision in a document going to
    management.
    """
    d = dec(value)
    if d is None:
        return "-"
    return f"{d.normalize():f}"


def signed(value) -> str:
    """A variance always carries its sign, so +2.1% never reads as 2.1%."""
    d = dec(value)
    if d is None:
        return "-"
    return f"{'+' if d > 0 else ''}{d}%"


def lead_time(supplier: dict) -> str:
    lo, hi = supplier.get("lead_time_min_weeks"), supplier.get("lead_time_max_weeks")
    if lo and hi:
        return f"{lo}-{hi} weeks"
    return f"{lo or hi} weeks" if (lo or hi) else "-"


def payment(supplier: dict) -> str:
    days = supplier.get("payment_terms_net_days")
    return f"Net {days}" if days else "-"


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------

def _context(run_id: str) -> dict | None:
    """Everything the package needs, read in one session."""
    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        if not run:
            return None
        comparison = session.get(Comparison, run.comparison_id)
        documents = [
            {"filename": d.original_filename, "status": d.status,
             "supplier": d.quote.supplier_id if d.quote else None}
            for d in session.scalars(
                select(SourceDocument).where(
                    SourceDocument.comparison_id == run.comparison_id))
        ]
        plants = sorted({d.plant for d in session.scalars(select(Demand)) if d.plant})
        strategy = session.scalar(
            select(CategoryStrategy)
            .where(CategoryStrategy.is_active.is_(True))
            .order_by(CategoryStrategy.uploaded_at.desc()))
        return {
            "result": run.result or {},
            "policy": run.policy_snapshot or {},
            "created_at": run.created_at,
            "engine_version": run.engine_version,
            "comparison_name": comparison.name if comparison else None,
            # The category is the strategy document's, when one is in force.
            # The comparison name is this batch's label, not the category.
            "category": strategy.category if strategy else None,
            "documents": documents,
            "plant": ", ".join(plants) if plants else "-",
        }


def build_package(run_id: str) -> dict | None:
    ctx = _context(run_id)
    if ctx is None:
        return None

    result = ctx["result"]
    policy = ctx["policy"]
    kpis = result.get("kpis", {})
    suppliers = result.get("suppliers", [])
    lines = result.get("lines", [])
    gates = result.get("gates", {})

    primary = next((s for s in suppliers if s.get("final_rank") == 1), None)
    secondary = next((s for s in suppliers if s.get("final_rank") == 2), None)
    not_recommended = [s for s in suppliers
                       if s.get("award_status") in ("NOT_RECOMMENDED", "EXCLUDED")]

    date_text = (ctx["created_at"].strftime("%d %B %Y")
                 if ctx["created_at"] else "-")

    # Materials, in the order the demand basket lists them.
    materials: list[tuple[str, str]] = []
    for line in lines:
        key = (line["cas_no"], line["material_name"])
        if key not in materials:
            materials.append(key)

    sections = [
        _executive_summary(result, kpis, suppliers, primary, secondary,
                           not_recommended, policy, ctx),
        _supplier_comparison(suppliers, lines, gates),
        _commercial_evaluation(suppliers, lines, materials),
        _benchmark_analysis(suppliers, lines, primary, kpis),
        _insights(suppliers, lines, materials, primary, secondary, policy, result),
        _strategy_alignment(suppliers, lines, gates, primary, secondary, policy,
                            result),
        _negotiation(result, primary, policy),
        _recommendation(suppliers, primary, secondary, policy),
        _supporting_documents(ctx, suppliers),
        _approval_information(),
    ]

    category = ctx["category"] or "Semiconductor Grade Wet Chemicals"

    return {
        "title": "SOURCING APPROVAL PACKAGE",
        "subtitle": category,
        "date_text": date_text,
        "footer": f"Sourcing Approval Package — {category}",
        "meta": fields([
            ("Category", category),
            ("Plant", ctx["plant"]),
            ("Materials", ", ".join(name for _, name in materials) or "-"),
            ("Prepared by", ""),
            ("Date", date_text),
            ("Approver", ""),
        ]),
        "sections": sections,
        "engine_version": ctx["engine_version"],
        "generated_at": result.get("generated_at"),
    }


# ---------------------------------------------------------------------------
# 1. Executive summary
# ---------------------------------------------------------------------------

def _executive_summary(result, kpis, suppliers, primary, secondary,
                       not_recommended, policy, ctx) -> dict:
    names = ", ".join(s["supplier_name"] for s in suppliers)
    # No site is configured unless PLANT_NAME is set, so the destination
    # clause drops out rather than printing a placeholder.
    plant = ctx.get("plant") or "-"
    destination = f" for delivery to {plant}" if plant != "-" else ""
    blocks = [
        para(f"Sourcing event summary: {kpis.get('supplier_count', 0)} quotations "
             f"received covering {kpis.get('material_count', 0)} materials"
             f"{destination}. Suppliers quoting: {names}."),
        para(f"Sourcing objective: secure the required volumes at the lowest total "
             f"landed cost while maintaining SEMI-grade compliance and keeping no "
             f"single vendor above "
             f"{pct(policy.get('max_vendor_share_pct')) or '60'}% of category "
             f"spend."),
    ]

    if primary:
        recommendation = (
            f"Recommendation: award primary supply to {primary['supplier_name']}.")
        if secondary:
            recommendation += (
                f" Retain {secondary['supplier_name']} as a qualified secondary "
                f"source.")
        if not_recommended:
            recommendation += (
                " " + ", ".join(s["supplier_name"] for s in not_recommended)
                + (" is" if len(not_recommended) == 1 else " are")
                + " not recommended for this basket.")
        blocks.append(para(recommendation))
    else:
        blocks.append(para(
            "Recommendation: no award. No supplier cleared every exclusion gate, "
            "so this basket cannot be awarded on the quotations received."))

    return {"number": 1, "title": "Executive Summary", "blocks": blocks}


# ---------------------------------------------------------------------------
# 2. Supplier comparison
# ---------------------------------------------------------------------------

def _compliance_gap_text(supplier, gates) -> str:
    gate1 = (gates.get(supplier["supplier_id"]) or [{}])[0]
    mandatory = visible((gate1.get("detail") or {}).get("gaps"))
    advisory = visible(supplier.get("advisory_gaps"))
    everything = mandatory + advisory
    if not everything:
        return "None"
    detail = ", ".join(
        [f"{code} (mandatory)" for code in mandatory] + advisory)
    return f"{len(everything)} ({detail})"


def _supplier_comparison(suppliers, lines, gates) -> dict:
    """Metrics down the side, suppliers across the top - the layout a buyer
    reads column by column when choosing between three quotes."""
    if not suppliers:
        return {"number": 2, "title": "Supplier Comparison",
                "blocks": [para("No supplier quotations were evaluated.")]}

    header = ["", *[s["supplier_name"] for s in suppliers]]

    def above_ceiling(supplier) -> str:
        own = [l for l in lines if l["supplier_id"] == supplier["supplier_id"]]
        return f"{sum(1 for l in own if l['above_ceiling'])} of {len(own)}"

    rows = [
        ["Total Landed Cost",
         *[f"EUR {eur(s['total_landed_cost_eur'])}" for s in suppliers]],
        ["Incoterm", *[s.get("incoterm") or "-" for s in suppliers]],
        ["Payment Terms", *[payment(s) for s in suppliers]],
        ["Lead Time", *[lead_time(s) for s in suppliers]],
        ["Quote Valid Until",
         *[(s.get("valid_until") or "-") + (" (expired)" if s.get("is_expired") else "")
           for s in suppliers]],
        ["Items Above Ceiling", *[above_ceiling(s) for s in suppliers]],
        ["Compliance Gaps",
         *[_compliance_gap_text(s, gates) for s in suppliers]],
        ["Award Status",
         *[(s.get("award_status") or "-").replace("_", " ").title()
           for s in suppliers]],
    ]
    return {"number": 2, "title": "Supplier Comparison",
            "blocks": [table(header, rows, numeric=list(range(1, len(header))))]}


# ---------------------------------------------------------------------------
# 3. Commercial evaluation
# ---------------------------------------------------------------------------

def _commercial_evaluation(suppliers, lines, materials) -> dict:
    header = ["Material", "Ceiling", *[s["supplier_name"] for s in suppliers]]
    rows = []
    for cas, name in materials:
        ceiling = next((l["ceiling_price_eur_l"] for l in lines
                        if l["cas_no"] == cas and l["ceiling_price_eur_l"]), None)
        row = [name, ceiling or "-"]
        for supplier in suppliers:
            line = next((l for l in lines
                         if l["cas_no"] == cas
                         and l["supplier_id"] == supplier["supplier_id"]), None)
            row.append(line["landed_price_per_l_eur"] if line else "not quoted")
        rows.append(row)

    blocks = [
        para("Landed unit price comparison (EUR / Litre). Landed price is the "
             "quoted price converted to EUR per litre and adjusted for the "
             "Incoterm's fixed freight percentage."),
        table(header, rows, numeric=list(range(1, len(header)))),
    ]

    offered = []
    for supplier in suppliers:
        for discount in supplier.get("discount_structure") or []:
            condition = (discount.get("condition_type") or "").replace("_", " ").lower()
            offered.append(
                f"{supplier['supplier_name']} offers {discount['discount_pct']}% "
                f"on {condition} orders")
    without = [s["supplier_name"] for s in suppliers
               if not (s.get("discount_structure") or [])]
    if without:
        offered.append(
            ", ".join(without)
            + (" offers" if len(without) == 1 else " offer") + " no basket discount")
    if offered:
        blocks.append(note(
            "Discounts: " + "; ".join(offered)
            + ". Payment terms and lead time are summarised in Section 2."))

    return {"number": 3, "title": "Commercial Evaluation", "blocks": blocks}


# ---------------------------------------------------------------------------
# 4. Benchmark analysis
# ---------------------------------------------------------------------------

def _historical_variance(supplier, lines) -> str:
    """Basket total against what the same basket cost historically.

    Only materials with purchase history are counted on either side, or the
    comparison would measure coverage rather than price.
    """
    own = [l for l in lines
           if l["supplier_id"] == supplier["supplier_id"]
           and l.get("historical_avg_eur_l")]
    if not own:
        return "no history"

    quoted = sum((dec(l["landed_price_per_l_eur"]) or Decimal(0))
                 * (dec(l["required_qty_l"]) or Decimal(0)) for l in own)
    historical = sum((dec(l["historical_avg_eur_l"]) or Decimal(0))
                     * (dec(l["required_qty_l"]) or Decimal(0)) for l in own)
    if not historical:
        return "no history"
    variance = (quoted - historical) / historical * 100
    return signed(variance.quantize(Decimal("0.1")))


def _benchmark_analysis(suppliers, lines, primary, kpis) -> dict:
    cheapest = min(
        (s for s in suppliers), key=lambda s: dec(s["total_landed_cost_eur"]),
        default=None)

    rows = []
    for supplier in suppliers:
        label = supplier["supplier_name"]
        if primary and supplier["supplier_id"] == primary["supplier_id"]:
            label += " (recommended)"
        elif cheapest and supplier["supplier_id"] == cheapest["supplier_id"]:
            label += " (lowest cost)"
        elif supplier.get("award_status") in ("NOT_RECOMMENDED", "EXCLUDED"):
            label += " (not recommended)"

        own = [l for l in lines if l["supplier_id"] == supplier["supplier_id"]]
        above = sum(1 for l in own if l["above_ceiling"])
        ceiling_cell = (
            f"{signed(supplier.get('ceiling_equivalent_variance_pct'))} "
            f"({above} item{'' if above == 1 else 's'} individually above)")

        rows.append([
            label,
            f"EUR {eur(supplier['total_landed_cost_eur'])}",
            _historical_variance(supplier, lines),
            ceiling_cell,
        ])

    return {
        "number": 4,
        "title": "Benchmark Analysis",
        "blocks": [
            table(["Supplier", "Total Landed Cost", "vs. Historical Average",
                   "vs. Category Ceiling (basket-equiv.)"], rows, numeric=[1, 2]),
            note(f"Ceiling-equivalent basket total: EUR "
                 f"{eur(kpis.get('ceiling_equivalent_total_eur'))}. Spread between "
                 f"cheapest and most expensive quotation: EUR "
                 f"{eur(kpis.get('spread_eur'))} ({kpis.get('spread_pct')}% of "
                 f"total basket value)."),
        ],
    }


# ---------------------------------------------------------------------------
# 5. Insights and findings
# ---------------------------------------------------------------------------

def _insights(suppliers, lines, materials, primary, secondary, policy,
              result) -> dict:
    found: list[str] = []

    # Widest spread across suppliers, and who sits above ceiling on it.
    widest, widest_span = None, Decimal(0)
    for cas, name in materials:
        prices = [dec(l["landed_price_per_l_eur"]) for l in lines
                  if l["cas_no"] == cas and l.get("landed_price_per_l_eur")]
        prices = [p for p in prices if p is not None]
        if len(prices) < 2:
            continue
        span = max(prices) - min(prices)
        if span > widest_span:
            widest, widest_span = (cas, name, min(prices), max(prices)), span
    if widest:
        cas, name, low, high = widest
        above = sorted({l["supplier_name"] for l in lines
                        if l["cas_no"] == cas and l["above_ceiling"]})
        sentence = (f"Price outlier: {name} shows the widest spread across "
                    f"suppliers (EUR {low} - {high}/L)")
        sentence += (f" and is priced above ceiling by {', '.join(above)}."
                     if above else " though no supplier is above its ceiling.")
        found.append(sentence)

    # Largest gap between the dearest quote for an item and the next dearest.
    biggest = None
    for cas, name in materials:
        priced = sorted(
            ((dec(l["landed_price_per_l_eur"]), l["supplier_name"]) for l in lines
             if l["cas_no"] == cas and l.get("landed_price_per_l_eur")),
            key=lambda p: p[0], reverse=True)
        if len(priced) < 2 or not priced[1][0]:
            continue
        gap = (priced[0][0] - priced[1][0]) / priced[1][0] * 100
        if biggest is None or gap > biggest[0]:
            biggest = (gap, name, priced[0][1], priced[0][0])
    if biggest and biggest[0] > 0:
        gap, name, supplier_name, price = biggest
        found.append(
            f"Unusual deviation: {supplier_name}'s {name} price (EUR {price}/L) is "
            f"{gap.quantize(Decimal('1'))}% above the second-highest quote, the "
            f"largest single-item gap in the basket.")

    # Concentration, if the whole basket went to one supplier.
    threshold = pct(policy.get("max_vendor_share_pct"))
    if primary:
        found.append(
            f"Risk observation: awarding the full basket to "
            f"{primary['supplier_name']} alone would raise its category spend "
            f"share above the {threshold}% concentration threshold set in the "
            f"category strategy.")

    # Lead time exposure on the retained second source.
    if secondary and primary:
        found.append(
            f"Risk observation: {secondary['supplier_name']}'s lead time "
            f"({lead_time(secondary)}) increases exposure if used as sole source; "
            f"acceptable as a secondary source alongside "
            f"{primary['supplier_name']}'s {lead_time(primary)}.")

    for supplier in suppliers:
        if supplier.get("is_expired"):
            found.append(
                f"Validity: {supplier['supplier_name']}'s quotation expired on "
                f"{supplier['valid_until']} and needs reconfirming before award.")

    if result.get("data_quality"):
        found.append(
            f"Data quality: {len(result['data_quality'])} extracted line item(s) "
            f"did not reconcile against the stated line total. Flagged for review, "
            f"not corrected.")

    if not found:
        found.append("No outliers or risks were identified in this basket.")

    return {"number": 5, "title": "Insights & Findings",
            "blocks": [bullets(found)]}


# ---------------------------------------------------------------------------
# 6. Category strategy alignment
# ---------------------------------------------------------------------------

def _strategy_alignment(suppliers, lines, gates, primary, secondary, policy,
                        result) -> dict:
    threshold = pct(policy.get("max_vendor_share_pct"))
    points: list[str] = []

    if primary and secondary:
        points.append(
            f"Dual-sourcing policy: satisfied — the recommendation retains two "
            f"suppliers ({primary['supplier_name']} primary, "
            f"{secondary['supplier_name']} secondary), keeping single-vendor "
            f"concentration below the {threshold}% threshold.")
    elif primary:
        points.append(
            f"Dual-sourcing policy: not satisfied — only "
            f"{primary['supplier_name']} cleared every gate, so the basket would "
            f"sit with a single vendor, above the {threshold}% threshold.")

    def above_count(supplier):
        own = [l for l in lines if l["supplier_id"] == supplier["supplier_id"]]
        return sum(1 for l in own if l["above_ceiling"]), len(own)

    if primary:
        above, total = above_count(primary)
        sentence = (
            f"Target/ceiling price alignment: {above} of {total} items from the "
            f"recommended supplier ({primary['supplier_name']}) sit above ceiling")
        sentence += (" and are flagged for renegotiation before award"
                     if above else "")
        if secondary:
            second_above, second_total = above_count(secondary)
            sentence += (f"; {second_above} of {second_total} items from the "
                         f"retained secondary source "
                         f"({secondary['supplier_name']}) exceed ceiling")
        points.append(sentence + ".")

    if secondary:
        points.append(
            f"Supplier diversity: retaining {secondary['supplier_name']} as a "
            f"qualified alternate reduces dependency on a single source of supply "
            f"for this plant.")

    if primary:
        gate1 = (gates.get(primary["supplier_id"]) or [{}])[0]
        met = visible(m["code"] for m in (gate1.get("detail") or {}).get("met", []))
        advisory = visible(primary.get("advisory_gaps"))
        if gate1.get("passed") and not advisory:
            points.append(
                f"Compliance checklist: {primary['supplier_name']} fully matches "
                f"the category strategy's compliance checklist "
                f"({', '.join(met) if met else 'all mandatory items stated'}).")
        else:
            points.append(
                f"Compliance checklist: {primary['supplier_name']} has "
                f"{_compliance_gap_text(primary, gates).lower()} against the "
                f"category strategy's checklist.")

    unapproved = (result.get("kpis") or {}).get("unapproved_suppliers") or []
    if unapproved:
        points.append(
            "Approved supplier list: " + ", ".join(unapproved)
            + " is not on the approved list for this category.")

    return {"number": 6, "title": "Category Strategy Alignment",
            "blocks": [bullets(points)]}


# ---------------------------------------------------------------------------
# 7. Negotiation opportunities
# ---------------------------------------------------------------------------

def _negotiation(result, primary, policy) -> dict:
    # Only the suppliers actually being awarded. A supplier excluded at a gate
    # is not someone the buyer is going back to the table with, so listing its
    # over-ceiling lines here reads as work to do rather than a closed question.
    awarded = {s["supplier_id"] for s in (result.get("suppliers") or [])
               if s.get("award_status") in ("PRIMARY", "SECONDARY")}
    candidates = [c for c in (result.get("renegotiation") or [])
                  if c["supplier_id"] in awarded]

    if not candidates:
        blocks = [para("No line item from an awarded supplier is priced above "
                       "its category strategy ceiling. There is no repricing "
                       "requirement before award.")]
        return {"number": 7, "title": "Negotiation Opportunities", "blocks": blocks}

    rows = []
    for row in candidates:
        best_elsewhere = min(
            (l["landed_price_per_l_eur"] for l in result.get("lines", [])
             if l["cas_no"] == row["cas_no"]
             and l["supplier_id"] != row["supplier_id"]),
            default=None, key=lambda v: dec(v) or Decimal("9" * 12))
        opportunity = f"{signed(row['gap_pct'])} against ceiling"
        if best_elsewhere:
            opportunity += f"; benchmark against EUR {best_elsewhere}/L elsewhere"
        rows.append([
            row["material_name"],
            row["supplier_name"],
            f"{signed(row['gap_pct'])} vs ceiling EUR {row['ceiling_price_eur_l']}/L",
            f"EUR {eur(row['annual_impact_eur'])} annual impact",
        ])

    blocks = [table(
        ["Item", "Supplier", "Gap vs. Ceiling / Benchmark", "Opportunity"], rows)]

    if primary:
        savings = sum(
            (dec(r["annual_impact_eur"]) or Decimal(0)) for r in candidates
            if r["supplier_id"] == primary["supplier_id"])
        if savings:
            blocks.append(note(
                f"Potential additional saving if {primary['supplier_name']}'s "
                f"flagged items are renegotiated to ceiling price: up to EUR "
                f"{eur(savings)}."))

    return {"number": 7, "title": "Negotiation Opportunities", "blocks": blocks}


# ---------------------------------------------------------------------------
# 8. Recommendation
# ---------------------------------------------------------------------------

DECISION = {
    "PRIMARY": "Primary Award",
    "SECONDARY": "Secondary / Dual-Source",
    "NOT_RECOMMENDED": "Not Recommended (back-up only)",
    "EXCLUDED": "Excluded",
}


def _recommendation(suppliers, primary, secondary, policy) -> dict:
    rows = []
    for supplier in suppliers:
        rows.append([
            str(supplier.get("final_rank") or "-"),
            supplier["supplier_name"],
            DECISION.get(supplier.get("award_status"), supplier.get("award_status") or "-"),
        ])

    blocks = [table(["Rank", "Supplier", "Decision"], rows)]

    if primary:
        rationale = f"Rationale: {primary['primary_reason']}"
        if secondary:
            rationale += (
                f" {secondary['supplier_name']} is retained as secondary source "
                f"under the category's dual-sourcing policy.")
        for supplier in suppliers:
            if supplier.get("award_status") in ("NOT_RECOMMENDED", "EXCLUDED"):
                rationale += f" {supplier['supplier_name']}: {supplier['primary_reason']}"
        blocks.append(para(rationale))

    allocation = pct(policy.get("primary_allocation_pct"))
    if secondary and allocation != "-":
        blocks.append(note(
            f"Award split assumes {allocation}% to the primary supplier. The "
            f"category strategy does not state a split; this is a configured "
            f"assumption, not a quoted or negotiated figure."))

    return {"number": 8, "title": "Recommendation", "blocks": blocks}


# ---------------------------------------------------------------------------
# 9. Supporting documents
# ---------------------------------------------------------------------------

def _supporting_documents(ctx, suppliers) -> dict:
    names = {s["supplier_id"]: s["supplier_name"] for s in suppliers}
    items = []
    for document in ctx["documents"]:
        label = names.get(document["supplier"])
        items.append(
            f"Supplier Quote — {label} ({document['filename']})" if label
            else f"Source document — {document['filename']}")
    if not items:
        items.append("No source documents are recorded against this run.")
    return {"number": 9, "title": "Supporting Documents",
            "blocks": [bullets(items)]}


# ---------------------------------------------------------------------------
# 10. Approval information
# ---------------------------------------------------------------------------

def _approval_information() -> dict:
    return {
        "number": 10,
        "title": "Approval Information",
        "blocks": [
            fields([
                ("Approver", ""),
                ("Approval Route", ""),
                ("Status", "[ ] Approved as recommended    "
                           "[ ] Approved with changes    "
                           "[ ] Rejected — return to buyer"),
                ("Approval Comments", ""),
            ]),
            signature(),
        ],
    }
