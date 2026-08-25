"""Bridge between the database and the pure engine.

Loads reference data and quotes out of Cloud SQL, converts them into the
engine's dataclasses, runs the evaluation and stores the result as an immutable
run. The engine itself never sees a session.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .engine.runner import evaluate
from .ingest.historical import vendor_spend
from .engine.types import (
    BenchmarkRef, DemandLine, DiscountInput, HistoricalRef, LineInput,
    MaterialRef, Policy, QuoteInput,
)
from .models import (
    ApprovedSupplier, Benchmark, ComplianceRequirement, Demand, EvaluationRun,
    FreightPolicy, HistoricalPrice, Material, PolicyConfig, Quote, Supplier,
)


def _d(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class EvaluationError(RuntimeError):
    pass


def build_policy(session: Session) -> Policy:
    cfg = {row.key: Decimal(str(row.value)) for row in session.scalars(select(PolicyConfig))}
    if not cfg:
        raise EvaluationError("policy configuration is empty; reference data not seeded")

    freight = {
        row.incoterm: (Decimal(str(row.freight_adj_pct)), row.basis_note)
        for row in session.scalars(select(FreightPolicy))
    }
    requirements = list(session.scalars(select(ComplianceRequirement)))

    return Policy(
        fx={"USD": cfg["fx_usd_eur"]},
        gallon_to_litre=cfg["gallon_to_litre"],
        price_rounding_dp=int(cfg["price_rounding_dp"]),
        line_total_tolerance_pct=cfg["line_total_tolerance_pct"],
        moq_overbuy_threshold_pct=cfg["moq_overbuy_threshold_pct"],
        ceiling_materiality_pct=cfg["ceiling_materiality_pct"],
        promotion_band_pct=cfg["promotion_band_pct"],
        primary_allocation_pct=cfg["primary_allocation_pct"],
        max_vendor_share_pct=cfg["max_vendor_share_pct"],
        freight_by_incoterm=freight,
        mandatory_requirements=tuple(
            r.code for r in requirements if r.tier == "MANDATORY"),
        advisory_requirements=tuple(
            r.code for r in requirements if r.tier == "ADVISORY"),
        requirement_labels={r.code: r.label for r in requirements},
    )


def load_reference(session: Session):
    materials = {
        m.cas_no: MaterialRef(m.cas_no, m.name, _d(m.density_kg_per_l))
        for m in session.scalars(select(Material))
    }
    demand = {
        d.cas_no: DemandLine(d.cas_no, _d(d.required_qty_l))
        for d in session.scalars(select(Demand))
    }
    benchmarks = {
        b.cas_no: BenchmarkRef(
            b.cas_no, _d(b.ceiling_price_eur_l), _d(b.target_price_eur_l))
        for b in session.scalars(select(Benchmark))
    }
    historical = {
        h.cas_no: HistoricalRef(
            cas_no=h.cas_no,
            avg_price_eur_l=_d(h.avg_price_eur_l),
            last_invoiced_price_eur_l=_d(h.last_invoiced_price_eur_l),
            min_price_eur_l=_d(h.min_price_eur_l),
            max_price_eur_l=_d(h.max_price_eur_l),
            po_line_count=h.po_line_count,
            period_from=h.period_from.isoformat() if h.period_from else None,
            period_to=h.period_to.isoformat() if h.period_to else None,
        )
        for h in session.scalars(select(HistoricalPrice))
    }
    return materials, demand, benchmarks, historical


def approved_keys(session: Session) -> set[str] | None:
    """Supplier keys the category strategy approves, or None when no list exists.

    None means no strategy has been uploaded yet, in which case every quoting
    supplier is treated as approved rather than all of them being flagged.
    """
    keys = {a.supplier_key for a in session.scalars(select(ApprovedSupplier))}
    return keys or None


def to_quote_input(
    quote: Quote, supplier: Supplier, approved: set[str] | None = None
) -> QuoteInput:
    lines = tuple(
        LineInput(
            quote_line_id=line.quote_line_id,
            cas_no=line.cas_no,
            quantity=_d(line.quantity),
            uom=line.uom or "L",
            unit_price=_d(line.unit_price),
            currency=line.currency or quote.currency or "EUR",
            line_total_stated=_d(line.line_total_stated),
            moq_qty=_d(line.moq_qty),
            moq_uom=line.moq_uom,
            moq_text=line.moq_text,
            supplier_description=line.supplier_description,
        )
        for line in sorted(quote.lines, key=lambda l: l.line_no)
        if line.cas_no and line.unit_price is not None
    )
    discounts = tuple(
        DiscountInput(
            discount_pct=_d(d.discount_pct),
            condition_type=d.condition_type,
            condition_text=d.condition_text,
            condition_threshold=_d(d.condition_threshold),
        )
        for d in quote.discounts
    )
    return QuoteInput(
        quote_id=quote.quote_id,
        supplier_id=quote.supplier_id,
        supplier_name=supplier.short_name if supplier else quote.supplier_id,
        currency=quote.currency or "EUR",
        incoterm=quote.incoterm,
        payment_terms_net_days=quote.payment_terms_net_days,
        lead_time_min_weeks=_d(quote.lead_time_min_weeks),
        lead_time_max_weeks=_d(quote.lead_time_max_weeks),
        lines=lines,
        discounts=discounts,
        compliance=quote.compliance or {},
        is_approved=(
            quote.supplier_id in approved if approved is not None else True),
    )


def run_evaluation(session: Session, comparison_id: str) -> EvaluationRun:
    quotes = list(session.scalars(
        select(Quote).where(
            Quote.comparison_id == comparison_id,
            Quote.superseded_by.is_(None),
        )
    ))
    if not quotes:
        raise EvaluationError("this comparison has no extracted quotes yet")

    # Every comparison in the spec assumes one quote per supplier. Two quotes
    # from the same supplier would make the ranking meaningless, so this is
    # reported rather than silently resolved.
    seen: dict[str, int] = {}
    for quote in quotes:
        seen[quote.supplier_id] = seen.get(quote.supplier_id, 0) + 1
    duplicates = [s for s, count in seen.items() if count > 1]
    if duplicates:
        raise EvaluationError(
            "more than one quote from the same supplier in this comparison: "
            + ", ".join(duplicates)
            + ". Remove the older document and evaluate again."
        )

    policy = build_policy(session)
    materials, demand, benchmarks, historical = load_reference(session)
    # Measured vendor share from the PO history, for the concentration check.
    incumbent = vendor_spend(session)

    approved = approved_keys(session)
    quote_inputs = []
    for quote in quotes:
        supplier = session.get(Supplier, quote.supplier_id)
        quote_inputs.append(to_quote_input(quote, supplier, approved))

    result = evaluate(
        quotes=quote_inputs,
        materials=materials,
        demand=demand,
        benchmarks=benchmarks,
        policy=policy,
        historical=historical,
        incumbent_spend=incumbent,
        engine_version=settings.engine_version,
    )

    run = EvaluationRun(
        comparison_id=comparison_id,
        quote_ids=[q.quote_id for q in quotes],
        policy_snapshot=policy.to_dict(),
        engine_version=settings.engine_version,
        result=result,
    )
    session.add(run)
    session.commit()
    return run
