"""Turn Document AI's strings into typed values, using Gemini on Vertex AI.

Document AI returns text: "6-8 weeks", "Net 60 days", "3% if all five items are
ordered together", "min. 5,000 L per shipment". This module converts those into
the numbers the engine needs, resolves a CAS number when a quote omits it, and
maps the quote text against the compliance checklist.

Two rules hold throughout:
  * The model returns structured JSON against a fixed schema. Nothing is parsed
    out of free text.
  * A compliance requirement that is not found is reported as a gap. It is
    never assumed to be met.
"""
from __future__ import annotations

import json
import logging

from .vertex import generate_json

log = logging.getLogger(__name__)

QUOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string"},
        "quote_no": {"type": "string", "nullable": True},
        "quote_date": {"type": "string", "nullable": True,
                       "description": "ISO date, YYYY-MM-DD"},
        "valid_until": {"type": "string", "nullable": True,
                        "description": "ISO date, YYYY-MM-DD"},
        "currency": {"type": "string", "description": "ISO 4217, e.g. EUR or USD"},
        "incoterm": {"type": "string", "nullable": True,
                     "description": "Three letter code only, e.g. DAP, FOB, EXW"},
        "incoterm_location": {"type": "string", "nullable": True},
        "payment_terms_net_days": {"type": "integer", "nullable": True},
        "lead_time_min_weeks": {"type": "number", "nullable": True},
        "lead_time_max_weeks": {"type": "number", "nullable": True},
        "discounts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "discount_pct": {"type": "number"},
                    "condition_type": {
                        "type": "string",
                        "enum": ["FULL_BASKET", "MIN_VALUE", "MIN_QTY", "UNCONDITIONAL"],
                    },
                    "condition_text": {"type": "string"},
                },
                "required": ["discount_pct", "condition_type", "condition_text"],
            },
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_no": {"type": "integer"},
                    "cas_no": {"type": "string", "nullable": True},
                    "supplier_description": {"type": "string", "nullable": True},
                    "quantity": {"type": "number", "nullable": True},
                    "uom": {"type": "string", "nullable": True,
                            "description": "One of L, KG or GAL"},
                    "unit_price": {"type": "number", "nullable": True},
                    "currency": {"type": "string", "nullable": True},
                    "line_total_stated": {"type": "number", "nullable": True},
                    "moq_qty": {"type": "number", "nullable": True},
                    "moq_uom": {"type": "string", "nullable": True},
                    "moq_text": {"type": "string", "nullable": True},
                },
                "required": ["line_no"],
            },
        },
    },
    "required": ["supplier_name", "currency", "lines"],
}

COMPLIANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "claimed": {"type": "boolean"},
                    "evidence_text": {
                        "type": "string", "nullable": True,
                        "description": "Sentence quoted verbatim from the quote",
                    },
                    "evidence_page": {"type": "integer", "nullable": True},
                },
                "required": ["code", "claimed"],
            },
        }
    },
    "required": ["claims"],
}

QUOTE_PROMPT = """You are normalizing a supplier quotation that has already been
read by an OCR extractor. Convert the extracted values into the structured form
below.

Rules:
- Never invent a value. If something is not present, return null.
- Units of measure must be one of L, KG or GAL. Map litre/liter/ltr to L,
  kilogram/kilo to KG, gallon/gal to GAL.
- Incoterm must be the three letter code alone; put the named place in
  incoterm_location.
- Payment terms become a whole number of days: "Net 60 days" is 60.
- Lead times become weeks: "6-8 weeks" is min 6 and max 8; "45 days" is min and
  max 6.4.
- A discount that applies only when the full range of items is ordered is
  FULL_BASKET. A discount with no condition is UNCONDITIONAL.
- CAS numbers look like 7664-93-9. Copy them exactly. If a line has no CAS
  number printed, infer it only from an unambiguous chemical name, otherwise
  return null.

The minimum order quantity block below describes the whole quote, and often
states a different rule per material ("1 IBC (1,000 L) per item; 1 pallet for
hydrofluoric acid"). Apply it to each line: set moq_qty and moq_uom where the
block gives a number for that material, and copy the phrase that applies into
moq_text. Where the block gives no number for a line, leave moq_qty null and
still copy the applicable phrase.

The discount block states the percentage and the condition attached to it.

Extracted header fields:
{header}

Extracted line items:
{lines}

Minimum order quantity block:
{moq_text}

Discount block:
{discount_text}

Document text:
{text}
"""

COMPLIANCE_PROMPT = """Check this supplier quotation against a compliance
checklist. For every requirement, decide whether the quotation actually states
it.

Rules:
- Set claimed true only when the document contains text supporting it, and quote
  that sentence verbatim in evidence_text.
- If a requirement is not mentioned, set claimed false and leave evidence_text
  null. Never treat silence as compliance.
- Return one entry for every requirement in the checklist.

Checklist:
{checklist}

Document text:
{text}
"""


def _block(extracted: dict, field: str) -> str:
    """One of the processor's free-text blocks, or an empty string."""
    return ((extracted.get("header") or {}).get(field) or {}).get("value") or ""


def normalize_quote(extracted: dict) -> dict:
    """Structured quote fields from the Document AI output."""
    header = extracted.get("header", {})
    prompt = QUOTE_PROMPT.format(
        header=json.dumps(header, indent=2),
        lines=json.dumps(extracted.get("lines", []), indent=2),
        moq_text=_block(extracted, "moq_text") or "(none extracted)",
        discount_text=_block(extracted, "discount_text") or "(none extracted)",
        text=(extracted.get("raw_text") or "")[:30000],
    )
    result = generate_json(prompt, QUOTE_SCHEMA)
    log.info("normalized quote for %s with %d lines",
             result.get("supplier_name"), len(result.get("lines", [])))
    return result


def map_compliance(extracted: dict, requirements: list[dict]) -> dict:
    """{code: {claimed, evidence_text, evidence_page}} for every requirement."""
    checklist = json.dumps(
        [{"code": r["code"], "label": r["label"], "look_for": r.get("match_hint")}
         for r in requirements], indent=2)
    # The processor isolates the compliance statements into their own block.
    # Prefer it over the whole document, and fall back when it is absent.
    source = _block(extracted, "compliance_text") or (extracted.get("raw_text") or "")
    prompt = COMPLIANCE_PROMPT.format(
        checklist=checklist, text=source[:30000])
    claims = generate_json(prompt, COMPLIANCE_SCHEMA).get("claims", [])

    mapped = {
        claim["code"]: {
            "claimed": bool(claim.get("claimed")),
            "evidence_text": claim.get("evidence_text"),
            "evidence_page": claim.get("evidence_page"),
        }
        for claim in claims
    }
    # Anything the model omitted is a gap, not a pass.
    for requirement in requirements:
        mapped.setdefault(
            requirement["code"],
            {"claimed": False, "evidence_text": None, "evidence_page": None})
    return mapped
