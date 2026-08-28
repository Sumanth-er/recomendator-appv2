"""Upload to evaluation-ready, in one place.

process_document runs the three stages that follow an upload: Document AI
extraction, Gemini normalization, and the automated validation checks. It is
called in the background so the browser is never waiting on it.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import telemetry
from ..models import (
    ComplianceRequirement, Quote, QuoteDiscount, QuoteLine, SourceDocument,
    Supplier,
)
from . import docai, storage
from .normalize import map_compliance, normalize_quote
from .validate import validate_quote

log = logging.getLogger(__name__)

UOM_ALIASES = {
    "L": "L", "LTR": "L", "LITRE": "L", "LITER": "L", "LITRES": "L", "LITERS": "L",
    "KG": "KG", "KGS": "KG", "KILOGRAM": "KG",
    "GAL": "GAL", "GALLON": "GAL", "GALLONS": "GAL",
}
MOQ_RE = re.compile(r"([\d.,]+)\s*(l|ltr|litre|liter|kg|gal|gallon)", re.I)


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def supplier_key(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", (name or "UNKNOWN").upper()).strip("_")
    return slug[:64] or "UNKNOWN"


def upsert_supplier(session: Session, name: str) -> Supplier:
    """Suppliers are never seeded - they come only from uploaded quotes."""
    key = supplier_key(name)
    supplier = session.get(Supplier, key)
    if not supplier:
        supplier = Supplier(
            supplier_id=key,
            legal_name=name or key,
            short_name=(name or key).split(",")[0].strip()[:100],
        )
        session.add(supplier)
        session.flush()
    return supplier


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _uom(value) -> str | None:
    if not value:
        return None
    return UOM_ALIASES.get(str(value).strip().upper(), str(value).strip().upper())


def _parse_moq(text: str | None) -> tuple[Decimal | None, str | None]:
    """Pull a quantity and unit out of free-text MOQ terms."""
    if not text:
        return None, None
    match = MOQ_RE.search(text)
    if not match:
        return None, None
    return _dec(match.group(1).replace(",", "")), _uom(match.group(2))


def _provenance(raw: dict, keys: dict[str, str]) -> dict:
    """Carry page and confidence through for every field we kept."""
    out = {}
    for source_key, field in keys.items():
        meta = raw.get(source_key)
        if isinstance(meta, dict):
            out[field] = {
                "raw_text": meta.get("value"),
                "confidence": meta.get("confidence"),
                "page": meta.get("page"),
            }
    return out


def process_document(session: Session, document_id: str) -> None:
    document = session.get(SourceDocument, document_id)
    if not document:
        log.warning("document %s disappeared before processing", document_id)
        return

    try:
        document.status = "PROCESSING"
        session.commit()

        content = storage.download_bytes(document.gcs_uri)

        # --- Stage 1: Document AI ---
        with telemetry.tracer().start_as_current_span("docai.extract") as span:
            extracted = docai.extract(content)
            span.set_attribute("docai.page_count", extracted.get("page_count") or 0)
            span.set_attribute("docai.unmapped_types",
                               len(extracted.get("unmapped_types") or []))
        document.page_count = extracted.get("page_count")
        document.docai_raw_gcs_uri = storage.upload_json(
            f"raw/{document.comparison_id}/{document_id}.json", extracted)
        session.commit()

        # --- Stage 2: Gemini normalization ---
        with telemetry.tracer().start_as_current_span("gemini.normalize") as span:
            normalized = normalize_quote(extracted)
        requirements = [
            {"code": r.code, "label": r.label, "match_hint": r.match_hint}
            for r in session.scalars(select(ComplianceRequirement))
        ]
        compliance = map_compliance(extracted, requirements)

        supplier = upsert_supplier(session, normalized.get("supplier_name"))

        quote = document.quote or Quote(
            document_id=document.document_id,
            comparison_id=document.comparison_id,
        )
        quote.supplier_id = supplier.supplier_id
        quote.quote_no = normalized.get("quote_no")
        quote.quote_date = _date(normalized.get("quote_date"))
        quote.valid_until = _date(normalized.get("valid_until"))
        quote.currency = (normalized.get("currency") or "EUR").upper()[:3]
        quote.incoterm = (normalized.get("incoterm") or "").upper()[:16] or None
        quote.incoterm_location = normalized.get("incoterm_location")
        quote.payment_terms_net_days = normalized.get("payment_terms_net_days")
        quote.payment_terms_text = (
            extracted.get("header", {}).get("payment_terms_text", {}).get("value"))
        quote.lead_time_min_weeks = _dec(normalized.get("lead_time_min_weeks"))
        quote.lead_time_max_weeks = _dec(normalized.get("lead_time_max_weeks"))
        quote.total_amount_stated = _dec(
            extracted.get("header", {}).get("total_amount", {}).get("value"))
        quote.compliance = compliance
        quote.provenance = _provenance(extracted.get("header", {}), {
            "supplier_name": "supplier_name", "quote_no": "quote_no",
            "quote_date": "quote_date", "valid_until": "valid_until",
            "currency": "currency", "incoterm": "incoterm",
            "payment_terms_text": "payment_terms_net_days",
            "lead_time_text": "lead_time_weeks",
            "total_amount": "total_amount_stated",
            "moq_text": "moq_terms",
            "discount_text": "discount_terms",
            "compliance_text": "compliance_statements",
        })
        session.add(quote)
        session.flush()

        # Replace lines rather than merging, so a reprocess is clean.
        for existing in list(quote.lines):
            session.delete(existing)
        for existing in list(quote.discounts):
            session.delete(existing)
        session.flush()

        raw_lines = extracted.get("lines") or []
        for index, line in enumerate(normalized.get("lines") or []):
            moq_qty, moq_uom = _parse_moq(line.get("moq_text"))
            raw = raw_lines[index] if index < len(raw_lines) else {}
            session.add(QuoteLine(
                quote_id=quote.quote_id,
                line_no=line.get("line_no") or index + 1,
                cas_no=line.get("cas_no"),
                supplier_description=line.get("supplier_description"),
                supplier_product_code=line.get("supplier_product_code"),
                quantity=_dec(line.get("quantity")),
                uom=_uom(line.get("uom")),
                unit_price=_dec(line.get("unit_price")),
                currency=(line.get("currency") or quote.currency or "EUR").upper()[:3],
                line_total_stated=_dec(line.get("line_total_stated")),
                moq_qty=_dec(line.get("moq_qty")) or moq_qty,
                moq_uom=_uom(line.get("moq_uom")) or moq_uom,
                moq_text=line.get("moq_text"),
                provenance=_provenance(raw, {
                    "cas_no": "cas_no", "quantity": "quantity",
                    "unit_price": "unit_price", "uom": "uom",
                    "line_total_stated": "line_total_stated",
                    "supplier_description": "supplier_description",
                }),
                flags=[],
            ))

        for discount in normalized.get("discounts") or []:
            session.add(QuoteDiscount(
                quote_id=quote.quote_id,
                discount_pct=_dec(discount.get("discount_pct")) or Decimal(0),
                condition_type=discount.get("condition_type") or "UNCONDITIONAL",
                condition_text=discount.get("condition_text") or "",
            ))

        session.flush()
        session.refresh(quote)

        # --- Stage 3: automated validation ---
        with telemetry.tracer().start_as_current_span("validate.quote"):
            validate_quote(session, quote)

        document.status = "READY"
        document.error_detail = None
        session.commit()
        log.info("document %s ready, supplier %s", document_id, supplier.supplier_id)

    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        telemetry.record_exception(exc)
        session.rollback()
        document = session.get(SourceDocument, document_id)
        if document:
            document.status = "FAILED"
            document.error_detail = f"{type(exc).__name__}: {exc}"
            session.commit()
        log.exception("processing failed for document %s", document_id)
