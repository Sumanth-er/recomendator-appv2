"""Automated checks that run after extraction.

There is no human approval step in this POC, so these checks are what stands
between a misread field and an approval memo. They never block and never
correct - they attach flags that the dashboard renders as badges beside the
number they concern.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Demand, PolicyConfig, Quote

REQUIRED_HEADER_FIELDS = (
    "currency", "incoterm", "payment_terms_net_days",
    "lead_time_min_weeks", "lead_time_max_weeks",
)


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def validate_quote(session: Session, quote: Quote) -> list[dict]:
    """Attach flags to the quote and its lines, returning them for the caller."""
    cfg = {row.key: Decimal(str(row.value))
           for row in session.scalars(select(PolicyConfig))}
    confidence_floor = cfg.get("extraction_confidence_threshold", Decimal("0.8"))
    tolerance = cfg.get("line_total_tolerance_pct", Decimal("0.5"))
    demand_cas = {d.cas_no for d in session.scalars(select(Demand))}

    findings: list[dict] = []
    header_flags: list[dict] = []

    for field in REQUIRED_HEADER_FIELDS:
        if getattr(quote, field, None) in (None, ""):
            header_flags.append({
                "severity": "warning",
                "field": field,
                "message": f"{field.replace('_', ' ')} was not found in the quote",
            })

    for field, meta in (quote.provenance or {}).items():
        score = _dec(meta.get("confidence"))
        if score is not None and 0 < score < confidence_floor:
            header_flags.append({
                "severity": "info",
                "field": field,
                "message": f"low extraction confidence ({score}) on {field}",
                "page": meta.get("page"),
            })

    # The processor extracts a document-level total. If the lines do not add up
    # to it, something was misread - reported, never reconciled silently.
    stated_total = _dec(quote.total_amount_stated)
    if stated_total:
        line_sum = sum(
            (_dec(l.line_total_stated) or Decimal(0)) for l in quote.lines)
        if line_sum and abs(line_sum - stated_total) / stated_total * 100 > tolerance:
            header_flags.append({
                "severity": "warning",
                "field": "total_amount",
                "message": (
                    f"quote total {stated_total} but the line totals sum to "
                    f"{line_sum}"
                ),
            })

    if header_flags:
        findings.extend({**f, "scope": "quote"} for f in header_flags)

    for line in quote.lines:
        flags: list[dict] = []

        if not line.cas_no:
            flags.append({
                "severity": "error",
                "field": "cas_no",
                "message": "no CAS number resolved, so this line cannot be "
                           "matched across suppliers and is excluded from totals",
            })
        elif line.cas_no not in demand_cas:
            flags.append({
                "severity": "warning",
                "field": "cas_no",
                "message": "CAS number is not part of the demand basket",
            })

        for field in ("unit_price", "quantity", "uom"):
            if getattr(line, field) in (None, ""):
                flags.append({
                    "severity": "error",
                    "field": field,
                    "message": f"{field.replace('_', ' ')} missing",
                })

        unit_price, quantity = _dec(line.unit_price), _dec(line.quantity)
        stated = _dec(line.line_total_stated)
        if unit_price is not None and quantity is not None and stated:
            recomputed = (unit_price * quantity).quantize(Decimal("0.01"))
            variance = abs((recomputed - stated) / stated * 100)
            if variance > tolerance:
                flags.append({
                    "severity": "warning",
                    "field": "line_total_stated",
                    "message": (
                        f"stated line total {stated} but unit price x quantity "
                        f"gives {recomputed}"
                    ),
                })

        for field, meta in (line.provenance or {}).items():
            score = _dec(meta.get("confidence"))
            if score is not None and 0 < score < confidence_floor:
                flags.append({
                    "severity": "info",
                    "field": field,
                    "message": f"low extraction confidence ({score})",
                    "page": meta.get("page"),
                })

        line.flags = flags
        findings.extend({**f, "scope": "line", "line_no": line.line_no} for f in flags)

    return findings
