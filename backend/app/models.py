"""Database schema.

This module is the single source of truth for the schema - there is no
separate .sql file. Tables are created on application startup if they do not
already exist (see db.init_db).

Design notes:
  * CAS number is the only cross-supplier match key.
  * Quoted values and derived values are stored in separate columns so the UI
    can label a derived number as derived.
  * An evaluation_run is an immutable snapshot: re-running never mutates an
    earlier run, so an approval package stays reproducible.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Reference data - seeded once, then read-only
# ---------------------------------------------------------------------------

class Material(Base):
    __tablename__ = "material"
    cas_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    grade: Mapped[str | None] = mapped_column(String(100))
    # NULL means kg <-> L conversion is impossible; the engine refuses the line
    # rather than guessing a density.
    density_kg_per_l: Mapped[float | None] = mapped_column(Numeric(10, 4))


class Supplier(Base):
    __tablename__ = "supplier"
    supplier_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(300))
    short_name: Mapped[str] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(100))
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True)


class Benchmark(Base):
    """Target and ceiling price per material, from the category strategy."""
    __tablename__ = "benchmark"
    cas_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("material.cas_no"), primary_key=True)
    target_price_eur_l: Mapped[float | None] = mapped_column(Numeric(14, 6))
    ceiling_price_eur_l: Mapped[float] = mapped_column(Numeric(14, 6))


class Demand(Base):
    """Required volume per material for the single plant in this POC."""
    __tablename__ = "demand"
    cas_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("material.cas_no"), primary_key=True)
    plant: Mapped[str] = mapped_column(String(100), default="")
    required_qty_l: Mapped[float] = mapped_column(Numeric(16, 4))


class FreightPolicy(Base):
    """Freight uplift applied to a normalized price, keyed on Incoterm."""
    __tablename__ = "freight_policy"
    incoterm: Mapped[str] = mapped_column(String(16), primary_key=True)
    freight_adj_pct: Mapped[float] = mapped_column(Numeric(7, 4))
    basis_note: Mapped[str] = mapped_column(Text)
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=True)


class ComplianceRequirement(Base):
    """Category strategy checklist.

    MANDATORY items drive Gate 1 (exclusion). ADVISORY items drive the
    compliance condition of the promotion rule. Flattening the two tiers
    breaks the promotion rule.
    """
    __tablename__ = "compliance_requirement"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    tier: Mapped[str] = mapped_column(String(16))  # MANDATORY | ADVISORY
    match_hint: Mapped[str | None] = mapped_column(Text)


class PolicyConfig(Base):
    """Rule thresholds and constants. Never hard-code these in the engine."""
    __tablename__ = "policy_config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Numeric(16, 6))
    unit: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    section_ref: Mapped[str | None] = mapped_column(String(32))


# ---------------------------------------------------------------------------
# Transactional data
# ---------------------------------------------------------------------------

class Comparison(Base):
    """A batch of quotes evaluated together. The batch is the selection -
    there is no separate 'pick which quotes' step."""
    __tablename__ = "comparison"
    comparison_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    documents: Mapped[list[SourceDocument]] = relationship(
        back_populates="comparison", cascade="all, delete-orphan")


class SourceDocument(Base):
    __tablename__ = "source_document"
    __table_args__ = (
        # Same file twice inside one comparison is deduplicated. The same file
        # in a different comparison is legitimately a new document.
        UniqueConstraint("comparison_id", "content_sha256", name="uq_doc_hash_per_comparison"),
    )
    document_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    comparison_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("comparison.comparison_id", ondelete="CASCADE"))
    doc_type: Mapped[str] = mapped_column(String(32), default="QUOTE")
    original_filename: Mapped[str] = mapped_column(String(400))
    content_sha256: Mapped[str] = mapped_column(String(64))
    gcs_uri: Mapped[str | None] = mapped_column(Text)
    docai_raw_gcs_uri: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="UPLOADED")
    error_detail: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    comparison: Mapped[Comparison] = relationship(back_populates="documents")
    quote: Mapped[Quote | None] = relationship(
        back_populates="document", cascade="all, delete-orphan", uselist=False)


class Quote(Base):
    __tablename__ = "quote"
    quote_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_document.document_id", ondelete="CASCADE"), unique=True)
    comparison_id: Mapped[str] = mapped_column(String(36), ForeignKey("comparison.comparison_id"))
    supplier_id: Mapped[str] = mapped_column(String(64), ForeignKey("supplier.supplier_id"))

    quote_no: Mapped[str | None] = mapped_column(String(100))
    quote_date: Mapped[str | None] = mapped_column(Date)
    valid_until: Mapped[str | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(String(3))
    incoterm: Mapped[str | None] = mapped_column(String(16))
    incoterm_location: Mapped[str | None] = mapped_column(String(120))
    payment_terms_net_days: Mapped[int | None] = mapped_column(Integer)
    payment_terms_text: Mapped[str | None] = mapped_column(Text)
    lead_time_min_weeks: Mapped[float | None] = mapped_column(Numeric(6, 2))
    lead_time_max_weeks: Mapped[float | None] = mapped_column(Numeric(6, 2))
    freight_estimate_text: Mapped[str | None] = mapped_column(Text)
    # Document-level total as printed, for cross-checking against the line sum
    total_amount_stated: Mapped[float | None] = mapped_column(Numeric(16, 4))

    # {code: {claimed, evidence_text, evidence_page}} - a code that is absent
    # or claimed=false is a gap, never assumed met.
    compliance: Mapped[dict] = mapped_column(JSON, default=dict)
    # {field_name: {page, confidence, raw_text}}
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    superseded_by: Mapped[str | None] = mapped_column(String(36))

    document: Mapped[SourceDocument] = relationship(back_populates="quote")
    lines: Mapped[list[QuoteLine]] = relationship(
        back_populates="quote", cascade="all, delete-orphan")
    discounts: Mapped[list[QuoteDiscount]] = relationship(
        back_populates="quote", cascade="all, delete-orphan")


class QuoteLine(Base):
    __tablename__ = "quote_line"
    quote_line_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    quote_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("quote.quote_id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)

    cas_no: Mapped[str | None] = mapped_column(String(32), ForeignKey("material.cas_no"))
    supplier_description: Mapped[str | None] = mapped_column(Text)
    supplier_product_code: Mapped[str | None] = mapped_column(String(100))

    quantity: Mapped[float | None] = mapped_column(Numeric(16, 4))
    uom: Mapped[str | None] = mapped_column(String(16))
    unit_price: Mapped[float | None] = mapped_column(Numeric(16, 6))
    currency: Mapped[str | None] = mapped_column(String(3))
    line_total_stated: Mapped[float | None] = mapped_column(Numeric(16, 4))

    moq_qty: Mapped[float | None] = mapped_column(Numeric(16, 4))
    moq_uom: Mapped[str | None] = mapped_column(String(16))
    moq_text: Mapped[str | None] = mapped_column(Text)

    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    flags: Mapped[list] = mapped_column(JSON, default=list)

    quote: Mapped[Quote] = relationship(back_populates="lines")


class QuoteDiscount(Base):
    __tablename__ = "quote_discount"
    discount_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    quote_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("quote.quote_id", ondelete="CASCADE"))
    discount_pct: Mapped[float] = mapped_column(Numeric(7, 4))
    condition_type: Mapped[str] = mapped_column(String(32))
    condition_text: Mapped[str] = mapped_column(Text)
    condition_threshold: Mapped[float | None] = mapped_column(Numeric(16, 4))

    quote: Mapped[Quote] = relationship(back_populates="discounts")


class CategoryStrategy(Base):
    """The uploaded category strategy document.

    Its four extracted parts land in benchmark, compliance_requirement,
    policy_config and approved_supplier. This row records where they came from,
    so the policy screen can say which document is in force.
    """
    __tablename__ = "category_strategy"
    strategy_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    category: Mapped[str] = mapped_column(String(200))
    version: Mapped[str | None] = mapped_column(String(64))
    effective_date: Mapped[str | None] = mapped_column(Date)
    source_filename: Mapped[str] = mapped_column(String(400))
    gcs_uri: Mapped[str | None] = mapped_column(Text)
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    applied_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApprovedSupplier(Base):
    """Suppliers the category strategy approves for this category.

    Populated only by a strategy upload. While this table is empty every
    quoting supplier is treated as approved, so the check does not fire before
    there is a list to check against.
    """
    __tablename__ = "approved_supplier"
    supplier_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(300))
    approved_from: Mapped[str | None] = mapped_column(Date)
    strategy_id: Mapped[str | None] = mapped_column(String(36))


class HistoricalPurchase(Base):
    """One purchase order line from the SAP BW extract (sheet 1).

    Sheet 2 gives the price benchmark the spec asks for directly. These lines
    add what sheet 2 cannot: which vendors the spend actually went to, which is
    the real input to the dual-sourcing and concentration check rather than an
    assumed split.
    """
    __tablename__ = "historical_purchase"
    purchase_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cas_no: Mapped[str | None] = mapped_column(String(32), ForeignKey("material.cas_no"))
    material_number: Mapped[str | None] = mapped_column(String(64))
    material_description: Mapped[str | None] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(200))
    plant: Mapped[str | None] = mapped_column(String(64))
    plant_name: Mapped[str | None] = mapped_column(String(200))
    vendor_number: Mapped[str | None] = mapped_column(String(64))
    vendor_name: Mapped[str | None] = mapped_column(String(300))
    vendor_key: Mapped[str | None] = mapped_column(String(64))
    po_number: Mapped[str | None] = mapped_column(String(64))
    po_item: Mapped[str | None] = mapped_column(String(32))
    document_date: Mapped[str | None] = mapped_column(Date)
    delivery_date: Mapped[str | None] = mapped_column(Date)
    order_quantity: Mapped[float | None] = mapped_column(Numeric(18, 4))
    base_uom: Mapped[str | None] = mapped_column(String(16))
    net_price: Mapped[float | None] = mapped_column(Numeric(18, 6))
    price_unit: Mapped[float | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    net_value_cur: Mapped[float | None] = mapped_column(Numeric(18, 4))
    net_value_eur: Mapped[float | None] = mapped_column(Numeric(18, 4))
    purchasing_group: Mapped[str | None] = mapped_column(String(64))
    incoterm: Mapped[str | None] = mapped_column(String(16))
    unit_price_eur_l: Mapped[float | None] = mapped_column(Numeric(18, 6))
    source_filename: Mapped[str | None] = mapped_column(String(400))


class HistoricalPrice(Base):
    """SAP BW-style extract of what was actually paid before.

    The second reference source in the spec. Matched to quote lines on CAS
    number, never on description or supplier product code.
    """
    __tablename__ = "historical_price"
    cas_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("material.cas_no"), primary_key=True)
    material_number: Mapped[str | None] = mapped_column(String(64))
    plant: Mapped[str | None] = mapped_column(String(100))
    avg_price_eur_l: Mapped[float | None] = mapped_column(Numeric(14, 6))
    min_price_eur_l: Mapped[float | None] = mapped_column(Numeric(14, 6))
    max_price_eur_l: Mapped[float | None] = mapped_column(Numeric(14, 6))
    last_invoiced_price_eur_l: Mapped[float | None] = mapped_column(Numeric(14, 6))
    last_invoiced_date: Mapped[str | None] = mapped_column(Date)
    period_from: Mapped[str | None] = mapped_column(Date)
    period_to: Mapped[str | None] = mapped_column(Date)
    po_line_count: Mapped[int | None] = mapped_column(Integer)
    material_description: Mapped[str | None] = mapped_column(String(300))
    source_filename: Mapped[str | None] = mapped_column(String(400))


class EvaluationRun(Base):
    """Immutable snapshot of one evaluation.

    `result` holds the full engine output: per-line evaluations, supplier
    totals, the gate trail, the promotion decision, allocation and
    renegotiation candidates. `policy_snapshot` freezes the thresholds and FX
    rate that were in force, so the run reproduces exactly.
    """
    __tablename__ = "evaluation_run"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    comparison_id: Mapped[str] = mapped_column(String(36), ForeignKey("comparison.comparison_id"))
    quote_ids: Mapped[list] = mapped_column(JSON)
    policy_snapshot: Mapped[dict] = mapped_column(JSON)
    engine_version: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RunNotes(Base):
    """Short written notes for one run's dashboard, drafted by the model.

    Every number on the dashboard comes from the run itself; this table holds
    only the sentences around them. Generated once per run and reused, because
    a run is immutable - the same figures would produce the same note.

    `schema_version` is what lets a newer dashboard sit on top of notes written
    by an older one: the reader takes the keys it knows and leaves the rest
    blank rather than failing. Nothing here is required for the dashboard to
    work; with this table empty every prose slot simply renders empty.
    """
    __tablename__ = "run_notes"
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("evaluation_run.run_id"), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApprovalPackage(Base):
    __tablename__ = "approval_package"
    package_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_run.run_id"))
    summary_md: Mapped[str] = mapped_column(Text)
    rendered_uri: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
