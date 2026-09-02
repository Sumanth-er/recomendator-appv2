"""Reference data.

These are the constants and policy values the requirements document defines -
densities, conversion factors, ceiling prices, the compliance checklist and the
rule thresholds. They are configuration the engine cannot run without, not
sample transactions. Suppliers and quotes are never seeded; those come only
from uploaded documents.

Seeding is idempotent. Existing rows are left untouched, with one deliberate
exception: the compliance checklist is reconciled against this file on every
start-up, because its wording is what the extraction prompt shows the model.
See seed_reference_data for what that does and does not overwrite.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Benchmark, CategoryStrategy, ComplianceRequirement, Demand, FreightPolicy,
    Material, PolicyConfig,
)

log = logging.getLogger(__name__)

MATERIALS = [
    ("7664-93-9", "Sulfuric Acid 98%", "SEMI Grade", 1.84),
    ("7722-84-1", "Hydrogen Peroxide 31%", "SEMI Grade", 1.11),
    ("7664-39-3", "Hydrofluoric Acid 49%", "SEMI Grade", 1.15),
    ("1336-21-6", "Ammonium Hydroxide 29%", "SEMI Grade", 0.90),
    ("67-63-0", "Isopropyl Alcohol 99.9%", "SEMI Grade", 0.785),
]

# Ceiling prices from the category strategy. Targets are not stated in the
# source document, so they are left unset rather than invented.
BENCHMARKS = [
    ("7664-93-9", None, 0.86),
    ("7722-84-1", None, 1.10),
    ("7664-39-3", None, 3.25),
    ("1336-21-6", None, 1.36),
    ("67-63-0", None, 1.63),
]

DEMAND = [
    ("7664-93-9", 50000),
    ("7722-84-1", 30000),
    ("7664-39-3", 8000),
    ("1336-21-6", 20000),
    ("67-63-0", 40000),
]

FREIGHT = [
    ("DAP", 0.0, "Delivered to site - freight already included in the price", False),
    ("FOB", 5.0, "Estimated from the quote's own freight figure", True),
    ("EXW", 9.5, "Midpoint of the quote's stated 9-10% freight estimate", True),
]

COMPLIANCE = [
    ("SEMI_C_CERT", "SEMI-C grade certification", "MANDATORY",
     "SEMI C1, SEMI-C, SEMI grade certification"),
    ("BATCH_COA", "certificate of analysis", "MANDATORY",
     "certificate of analysis, CoA per batch, Certificate of Analysis (CoA), batch CoA"),
    ("SDS_LANGUAGE", "Safety data sheet", "ADVISORY",
     "SDS, safety data sheet, Safety Data Sheet (SDS)"),
    ("ISO_9001", "ISO 9001", "ADVISORY", "ISO 9001"),
    ("ISO_14001", "ISO 14001", "ADVISORY", "ISO 14001"),
    ("REACH", "REACH registration", "ADVISORY", "REACH, EC 1907/2006"),
    ("TSCA", "TSCA compliance", "ADVISORY", "TSCA, Toxic Substances Control Act"),
]

POLICY = [
    ("fx_usd_eur", 0.925, "rate", "USD to EUR rate fixed for this POC", "3.1"),
    ("gallon_to_litre", 3.7854, "L", "US gallon to litre conversion", "3.1"),
    ("price_rounding_dp", 2, "digits",
     "Decimals shown for a per-litre price. Display only - totals are extended "
     "from the unrounded price, which is how the worked examples in sections "
     "3.3 and 4.1 relate to each other", "3.1"),
    ("line_total_tolerance_pct", 0.5, "%",
     "unit price x quantity may differ from the stated line total by this much "
     "before it is flagged", "7"),
    ("moq_overbuy_threshold_pct", 10, "%",
     "Gate 2: a supplier fails if any line forces an overbuy above this", "5.2"),
    ("ceiling_materiality_pct", 5, "%",
     "Gate 3: a supplier drops from primary ranking above this variance against "
     "the ceiling-equivalent basket total", "5.3"),
    ("promotion_band_pct", 10, "%",
     "Promotion rule: cost gap must be inside this band", "5.5"),
    ("primary_allocation_pct", 60, "%",
     "Share of basket spend assumed to go to the primary award when a secondary "
     "source is retained. Not stated in the source document - assumption", "5.5"),
    ("max_vendor_share_pct", 60, "%",
     "Dual-sourcing concentration threshold: no vendor above this share", "5.5"),
    ("min_supplier_count", 2, "count", "Dual-sourcing policy minimum", "5.5"),
    ("extraction_confidence_threshold", 0.80, "score",
     "Extracted fields below this confidence are flagged on the dashboard", "7"),
]


def seed_reference_data(session: Session) -> None:
    added = 0
    updated = 0

    for cas, name, grade, density in MATERIALS:
        if not session.get(Material, cas):
            session.add(Material(cas_no=cas, name=name, grade=grade,
                                 density_kg_per_l=density))
            added += 1

    for cas, target, ceiling in BENCHMARKS:
        if not session.get(Benchmark, cas):
            session.add(Benchmark(cas_no=cas, target_price_eur_l=target,
                                  ceiling_price_eur_l=ceiling))
            added += 1

    for cas, qty in DEMAND:
        if not session.get(Demand, cas):
            session.add(Demand(cas_no=cas, required_qty_l=qty,
                               plant=settings.plant_name))
            added += 1

    for incoterm, pct, note, est in FREIGHT:
        if not session.get(FreightPolicy, incoterm):
            session.add(FreightPolicy(incoterm=incoterm, freight_adj_pct=pct,
                                      basis_note=note, is_estimate=est))
            added += 1

    # The compliance checklist is the one block of reference data that is
    # reconciled rather than only inserted. Its label and match_hint are what
    # the extraction prompt shows the model, so editing them in this file has
    # to reach a database that was seeded months ago - otherwise the wording
    # driving extraction is whatever shipped first, and changing it here looks
    # like it does nothing.
    strategy_owns_checklist = session.scalar(
        select(CategoryStrategy).where(CategoryStrategy.is_active.is_(True))
    ) is not None

    for code, label, tier, hint in COMPLIANCE:
        row = session.get(ComplianceRequirement, code)
        if not row:
            session.add(ComplianceRequirement(code=code, label=label, tier=tier,
                                              match_hint=hint))
            added += 1
            continue

        # A strategy upload can set label and tier; it never sets match_hint.
        # So the hint is always ours to correct, and the other two only while
        # no strategy is in force - re-uploading the strategy is what changes
        # them after that.
        if row.match_hint != hint:
            row.match_hint = hint
            updated += 1
        if not strategy_owns_checklist:
            if row.label != label:
                row.label = label
                updated += 1
            if row.tier != tier:
                row.tier = tier
                updated += 1

    for key, value, unit, desc, ref in POLICY:
        if not session.get(PolicyConfig, key):
            session.add(PolicyConfig(key=key, value=value, unit=unit,
                                     description=desc, section_ref=ref))
            added += 1

    if added or updated:
        session.commit()
        log.info("reference data: %d rows seeded, %d reconciled", added, updated)
