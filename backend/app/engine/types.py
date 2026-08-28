"""Value types for the calculation engine.

The engine takes plain dataclasses in and returns plain dataclasses out. It
imports nothing from the database, Google Cloud or any model API, which is what
makes it testable in isolation and reproducible run to run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class MaterialRef:
    cas_no: str
    name: str
    density_kg_per_l: Decimal | None


@dataclass(frozen=True)
class Policy:
    """Thresholds and constants, frozen for the duration of one run."""
    fx: dict[str, Decimal]                  # e.g. {"USD": Decimal("0.925")}
    gallon_to_litre: Decimal
    price_rounding_dp: int
    line_total_tolerance_pct: Decimal
    moq_overbuy_threshold_pct: Decimal
    ceiling_materiality_pct: Decimal
    promotion_band_pct: Decimal
    primary_allocation_pct: Decimal
    max_vendor_share_pct: Decimal
    freight_by_incoterm: dict[str, tuple[Decimal, str]]   # pct, basis note
    mandatory_requirements: tuple[str, ...]
    advisory_requirements: tuple[str, ...]
    # code -> human label, for the compliance checklist comparison
    requirement_labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "fx": {k: str(v) for k, v in self.fx.items()},
            "gallon_to_litre": str(self.gallon_to_litre),
            "price_rounding_dp": self.price_rounding_dp,
            "line_total_tolerance_pct": str(self.line_total_tolerance_pct),
            "moq_overbuy_threshold_pct": str(self.moq_overbuy_threshold_pct),
            "ceiling_materiality_pct": str(self.ceiling_materiality_pct),
            "promotion_band_pct": str(self.promotion_band_pct),
            "primary_allocation_pct": str(self.primary_allocation_pct),
            "max_vendor_share_pct": str(self.max_vendor_share_pct),
            "freight_by_incoterm": {
                k: {"pct": str(v[0]), "basis": v[1]}
                for k, v in self.freight_by_incoterm.items()
            },
            "mandatory_requirements": list(self.mandatory_requirements),
            "advisory_requirements": list(self.advisory_requirements),
        }


@dataclass(frozen=True)
class LineInput:
    quote_line_id: str
    cas_no: str
    quantity: Decimal | None
    uom: str
    unit_price: Decimal
    currency: str
    line_total_stated: Decimal | None = None
    moq_qty: Decimal | None = None
    moq_uom: str | None = None
    moq_text: str | None = None
    supplier_description: str | None = None


@dataclass(frozen=True)
class DiscountInput:
    discount_pct: Decimal
    condition_type: str          # FULL_BASKET | MIN_VALUE | MIN_QTY | UNCONDITIONAL
    condition_text: str
    condition_threshold: Decimal | None = None


@dataclass(frozen=True)
class QuoteInput:
    quote_id: str
    supplier_id: str
    supplier_name: str
    currency: str
    incoterm: str | None
    payment_terms_net_days: int | None
    lead_time_min_weeks: Decimal | None
    lead_time_max_weeks: Decimal | None
    lines: tuple[LineInput, ...]
    discounts: tuple[DiscountInput, ...] = ()
    # Traceability only - the dashboard shows when a quote was issued and how
    # long it stands. Nothing in the engine ranks on either, so both default to
    # None and a quote that omits them still evaluates.
    quote_date: date | None = None
    valid_until: date | None = None
    # From the category strategy's approved supplier list (spec 2.3)
    is_approved: bool = True
    # {code: {"claimed": bool, "evidence_text": str|None, "evidence_page": int|None}}
    compliance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DemandLine:
    cas_no: str
    required_qty_l: Decimal


@dataclass(frozen=True)
class BenchmarkRef:
    cas_no: str
    ceiling_price_eur_l: Decimal
    target_price_eur_l: Decimal | None = None


@dataclass(frozen=True)
class HistoricalRef:
    """Per-material benchmark from the SAP BW extract (spec 2.2)."""
    cas_no: str
    avg_price_eur_l: Decimal | None
    last_invoiced_price_eur_l: Decimal | None = None
    min_price_eur_l: Decimal | None = None
    max_price_eur_l: Decimal | None = None
    po_line_count: int | None = None
    period_from: str | None = None
    period_to: str | None = None
