"""Document AI extraction.

FIELD_MAP and LINE_ITEM_MAP are the only place in the codebase that knows the
processor's entity names. Everything downstream works in this application's own
field names, so a processor schema change is a one-file change.

Confidence and page number travel with every field and are stored in the
provenance column, so a number on the dashboard can always be traced back to
where it was read from.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Processor schema
# ---------------------------------------------------------------------------
# Left-hand side is the entity type configured in the quotation processor.
#
# Three of these are free-text blocks rather than single values. They are not
# parsed here - they are handed to the normalizer, which turns them into typed
# fields against a fixed schema.

FIELD_MAP = {
    "supplier_name": "supplier_name",
    "quote_number": "quote_no",
    "quote_date": "quote_date",
    "validity_date": "valid_until",
    "currency": "currency",
    "incoterm": "incoterm",
    "payment_terms": "payment_terms_text",
    "lead_time": "lead_time_text",
    "total_amount": "total_amount",
    # Free-text blocks
    "compliance_text_block": "compliance_text",
    "discount_text_block": "discount_text",
    "moq_text_block": "moq_text",
}

# Properties of the nested line_item entity.
LINE_ITEM_MAP = {
    "cas_number": "cas_no",
    "item_description": "supplier_description",
    "quantity": "quantity",
    "unit_of_measure": "uom",
    "unit_price": "unit_price",
    "line_total": "line_total_stated",
}

LINE_ITEM_TYPES = {"line_item"}


def _page_of(entity: Any) -> int | None:
    try:
        refs = entity.page_anchor.page_refs
        if refs:
            return int(refs[0].page) + 1
    except Exception:  # noqa: BLE001 - page anchors are optional
        pass
    return None


def _value_of(entity: Any) -> str:
    """Prefer the processor's normalized value, fall back to the raw mention.

    Number-typed fields come back with a parsed value, so taking that avoids
    re-parsing thousands separators and decimal commas here.
    """
    normalized = getattr(entity, "normalized_value", None)
    if normalized is not None:
        text = (getattr(normalized, "text", "") or "").strip()
        if text:
            return text
    return (entity.mention_text or "").strip()


def _type_of(entity: Any) -> str:
    """Entity type, lowercased with spaces and hyphens folded to underscores.

    Guards against a schema field named "unit price" rather than "unit_price".
    """
    return (entity.type_ or "").strip().lower().replace(" ", "_").replace("-", "_")


def extract(content: bytes, mime_type: str = "application/pdf") -> dict:
    """Run the configured processor and return the mapped dictionary."""
    if not settings.docai_processor_id:
        raise RuntimeError("DOCAI_PROCESSOR_ID is not set")

    from google.api_core.client_options import ClientOptions
    from google.cloud import documentai

    client = documentai.DocumentProcessorServiceClient(
        client_options=ClientOptions(
            api_endpoint=f"{settings.docai_location}-documentai.googleapis.com")
    )
    name = client.processor_path(
        settings.project_id, settings.docai_location, settings.docai_processor_id)

    request = documentai.ProcessRequest(
        name=name,
        raw_document=documentai.RawDocument(content=content, mime_type=mime_type),
    )
    document = client.process_document(request=request).document
    return map_document(document)


def map_document(document: Any) -> dict:
    """Translate a Document AI response into this application's field names.

    Split out from extract() so the processor schema can be tested without
    calling Google.

    Shape:
      {
        "header": {field: {"value", "confidence", "page"}},
        "lines":  [ {field: {"value", "confidence", "page"}} ],
        "page_count": int,
        "raw_text": str,
        "unmapped_types": [str],
      }
    """
    header: dict[str, dict] = {}
    lines: list[dict] = []
    unmapped: set[str] = set()

    for entity in document.entities:
        etype = _type_of(entity)

        if etype in LINE_ITEM_TYPES:
            line: dict[str, dict] = {}
            for prop in entity.properties:
                prop_type = _type_of(prop)
                key = LINE_ITEM_MAP.get(prop_type)
                if not key:
                    unmapped.add(f"{etype}.{prop_type}")
                    continue
                line[key] = {
                    "value": _value_of(prop),
                    "confidence": round(float(prop.confidence or 0), 4),
                    "page": _page_of(prop),
                }
            if line:
                lines.append(line)
            continue

        key = FIELD_MAP.get(etype)
        if not key:
            unmapped.add(etype)
            continue
        # Document AI can emit the same type more than once; keep the first.
        if key not in header:
            header[key] = {
                "value": _value_of(entity),
                "confidence": round(float(entity.confidence or 0), 4),
                "page": _page_of(entity),
            }

    log.info("document ai returned %d header fields and %d line items",
             len(header), len(lines))
    if unmapped:
        # Not an error - the processor may emit fields this POC does not use.
        # Logged so a schema change is visible rather than silently dropped.
        log.warning("unmapped Document AI entity types: %s", ", ".join(sorted(unmapped)))

    return {
        "header": header,
        "lines": lines,
        "page_count": len(document.pages),
        "raw_text": document.text or "",
        "unmapped_types": sorted(unmapped),
    }
