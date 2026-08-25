"""Category strategy document - the one-time policy setup (spec 2.3).

Four things come out of it:

  * target and ceiling price per material  -> benchmark        (4.1, 5.3)
  * compliance checklist with its tiers    -> compliance_requirement (5.1, 5.5)
  * dual-sourcing / concentration threshold-> policy_config    (5.5)
  * approved supplier list                 -> approved_supplier(2.3)

Until a document is uploaded the seeded defaults stand, so the system is usable
from a cold start. Uploading replaces only the parts the document actually
states; anything it is silent about keeps its default rather than being wiped.

There is no Document AI processor for this document, so Gemini reads it
directly - a PDF as bytes, a .docx as text pulled from its XML.
"""
from __future__ import annotations

import logging
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ApprovedSupplier, Benchmark, CategoryStrategy, ComplianceRequirement,
    Material, PolicyConfig,
)
from .pipeline import supplier_key
from .vertex import generate_json, pdf_part

log = logging.getLogger(__name__)

# The checklist codes the engine knows. Gate 1 and the promotion rule are keyed
# on these, so the model chooses among them rather than inventing new ones.
# Anything else the document mentions is returned separately and reported.
KNOWN_CODES = (
    "SEMI_C_CERT", "BATCH_COA", "SDS_LANGUAGE",
    "ISO_9001", "ISO_14001", "REACH", "TSCA",
)

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "version": {"type": "string", "nullable": True},
        "effective_date": {"type": "string", "nullable": True,
                           "description": "ISO date, YYYY-MM-DD"},
        "materials": {
            "type": "array",
            "description": "Target and ceiling price per material, EUR per litre",
            "items": {
                "type": "object",
                "properties": {
                    "cas_no": {"type": "string",
                               "description": "CAS number, e.g. 7664-93-9"},
                    "material_name": {"type": "string", "nullable": True},
                    "target_price_eur_l": {"type": "number", "nullable": True},
                    "ceiling_price_eur_l": {"type": "number", "nullable": True},
                },
                "required": ["cas_no"],
            },
        },
        "compliance_requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": list(KNOWN_CODES)},
                    "label": {"type": "string"},
                    "tier": {"type": "string", "enum": ["MANDATORY", "ADVISORY"]},
                },
                "required": ["code", "tier"],
            },
        },
        "additional_requirements": {
            "type": "array",
            "description": "Checklist items that do not fit the known codes",
            "items": {"type": "string"},
        },
        "max_vendor_share_pct": {
            "type": "number", "nullable": True,
            "description": "Concentration threshold, e.g. 60 for no vendor above 60%",
        },
        "min_supplier_count": {"type": "integer", "nullable": True},
        "approved_suppliers": {
            "type": "array",
            "items": {"type": "string", "description": "Supplier legal name"},
        },
    },
    "required": ["category"],
}

PROMPT = """Read this category strategy document and extract the policy it sets.

Rules:
- Never invent a value. If the document does not state something, omit it or
  return null. Values that are absent keep their existing setting, so a guess
  silently changes policy.
- Prices are EUR per litre. If a price is given per kilogram or per gallon,
  do not convert it - omit it and mention the material in
  additional_requirements.
- A requirement is MANDATORY only when the document says a supplier failing it
  is excluded or that it is required. Otherwise it is ADVISORY.
- For the compliance checklist use only the codes offered. A checklist item
  that does not match one of them goes in additional_requirements as free text.
- The concentration threshold is the maximum share of spend one vendor may
  hold, as a number: "no vendor above 60% of spend" is 60.
- approved_suppliers is the list of suppliers approved for this category. Use
  the legal names exactly as written.
"""


def _text_from_docx(content: bytes) -> str:
    """Plain text from a .docx, using only the standard library.

    A .docx is a zip; the body text lives in word/document.xml as <w:t> runs.
    """
    with zipfile.ZipFile(BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    # Paragraph and row boundaries become newlines so the model sees structure.
    xml = re.sub(r"</w:(p|tr)>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.DOTALL)
    text = "".join(runs)
    if not text.strip():
        # Fall back to stripping every tag rather than returning nothing.
        text = re.sub(r"<[^>]+>", " ", xml)
    return re.sub(r"\n{3,}", "\n\n", text)


def extract_strategy(content: bytes, filename: str, mime_type: str | None) -> dict:
    """Read the document with Gemini and return the structured policy."""
    name = (filename or "").lower()
    mime = (mime_type or "").lower()

    if name.endswith(".pdf") or "pdf" in mime:
        contents = [pdf_part(content), PROMPT]
    elif name.endswith(".docx") or "wordprocessingml" in mime:
        contents = [PROMPT, "\n\nDocument text:\n" + _text_from_docx(content)]
    else:
        text = content.decode("utf-8", errors="replace")
        contents = [PROMPT, "\n\nDocument text:\n" + text]

    result = generate_json(contents, SCHEMA)
    log.info("category strategy extracted: %d materials, %d requirements, "
             "%d approved suppliers",
             len(result.get("materials") or []),
             len(result.get("compliance_requirements") or []),
             len(result.get("approved_suppliers") or []))
    return result


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def apply_strategy(
    session: Session, extracted: dict, filename: str, gcs_uri: str | None = None
) -> dict:
    """Write the extracted policy into the reference tables.

    Only what the document states is changed. A material it does not mention
    keeps its existing ceiling; a threshold it omits keeps its default.
    """
    summary: dict = {
        "materials_updated": [], "materials_unknown": [],
        "requirements_updated": [], "thresholds_updated": [],
        "approved_suppliers": [], "ignored": list(extracted.get(
            "additional_requirements") or []),
    }
    known_cas = {m.cas_no for m in session.scalars(select(Material))}

    # --- target and ceiling prices ---
    for item in extracted.get("materials") or []:
        cas = (item.get("cas_no") or "").strip()
        if cas not in known_cas:
            summary["materials_unknown"].append(cas or item.get("material_name"))
            continue
        target = _dec(item.get("target_price_eur_l"))
        ceiling = _dec(item.get("ceiling_price_eur_l"))
        if target is None and ceiling is None:
            continue
        row = session.get(Benchmark, cas)
        if not row:
            if ceiling is None:
                # A benchmark row cannot exist without a ceiling.
                summary["materials_unknown"].append(cas)
                continue
            row = Benchmark(cas_no=cas, ceiling_price_eur_l=ceiling)
            session.add(row)
        if target is not None:
            row.target_price_eur_l = target
        if ceiling is not None:
            row.ceiling_price_eur_l = ceiling
        summary["materials_updated"].append(
            {"cas_no": cas, "target": str(target) if target else None,
             "ceiling": str(ceiling) if ceiling else None})

    # --- compliance checklist ---
    for item in extracted.get("compliance_requirements") or []:
        code = (item.get("code") or "").strip().upper()
        if code not in KNOWN_CODES:
            summary["ignored"].append(code)
            continue
        row = session.get(ComplianceRequirement, code)
        if not row:
            row = ComplianceRequirement(code=code, label=item.get("label") or code,
                                        tier=item.get("tier") or "ADVISORY")
            session.add(row)
        else:
            if item.get("label"):
                row.label = item["label"]
            row.tier = item.get("tier") or row.tier
        summary["requirements_updated"].append({"code": code, "tier": row.tier})

    # --- dual-sourcing policy ---
    for key, value in (
        ("max_vendor_share_pct", _dec(extracted.get("max_vendor_share_pct"))),
        ("min_supplier_count", _dec(extracted.get("min_supplier_count"))),
    ):
        if value is None:
            continue
        row = session.get(PolicyConfig, key)
        if row:
            row.value = value
            summary["thresholds_updated"].append({"key": key, "value": str(value)})

    # --- approved supplier list, replaced wholesale ---
    names = [n.strip() for n in (extracted.get("approved_suppliers") or []) if n.strip()]
    if names:
        for existing in session.scalars(select(ApprovedSupplier)):
            session.delete(existing)
        session.flush()
        for name in names:
            session.add(ApprovedSupplier(
                supplier_key=supplier_key(name), legal_name=name))
            summary["approved_suppliers"].append(name)

    # --- record which document is in force ---
    for previous in session.scalars(
            select(CategoryStrategy).where(CategoryStrategy.is_active.is_(True))):
        previous.is_active = False

    strategy = CategoryStrategy(
        category=extracted.get("category") or "Semiconductor Grade Wet Chemicals",
        version=extracted.get("version"),
        effective_date=_date(extracted.get("effective_date")),
        source_filename=filename,
        gcs_uri=gcs_uri,
        extracted=extracted,
        applied_summary=summary,
    )
    session.add(strategy)
    session.commit()

    log.info("category strategy applied from %s: %s", filename, summary)
    return {"strategy_id": strategy.strategy_id, **summary}
