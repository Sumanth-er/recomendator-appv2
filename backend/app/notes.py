"""Short written notes for the dashboard.

The dashboard's numbers all come from the stored run. This module supplies only
the sentences beside them - why a supplier was recommended, what a gate step
means, what to double-check. The model writes prose; it is never asked for a
figure, and anything it returns that is not a string is dropped.

Notes are generated once per run and stored, because a run is immutable: the
same figures would produce the same note. Failure is not an error - the caller
gets empty notes and the dashboard renders without them.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from .config import settings
from .models import EvaluationRun, RunNotes

log = logging.getLogger(__name__)

# Bumped when the shape below changes. A dashboard reads the keys it knows and
# leaves the rest blank, so an older row never breaks a newer page.
SCHEMA_VERSION = 1

EMPTY: dict = {}

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "object",
            "properties": {
                "recommendation": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["recommendation", "why"],
        },
        "kpi_notes": {
            "type": "object",
            "properties": {
                "gap": {"type": "string"},
                "target": {"type": "string"},
                "savings": {"type": "string"},
            },
            "required": ["gap", "target", "savings"],
        },
        "suppliers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "supplier_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["supplier_id", "note"],
            },
        },
        "method": {
            "type": "object",
            "properties": {
                "compliance": {"type": "string"},
                "target_price": {"type": "string"},
                "rank": {"type": "string"},
                "promotion": {"type": "string"},
            },
            "required": ["compliance", "target_price", "rank", "promotion"],
        },
        "alignment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "detail"],
            },
        },
        "negotiation": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cas_no": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["cas_no", "note"],
            },
        },
        "footer": {
            "type": "object",
            "properties": {
                "covers": {"type": "string"},
                "comparable": {"type": "string"},
                "double_check": {"type": "string"},
            },
            "required": ["covers", "comparable", "double_check"],
        },
    },
    "required": ["headline", "kpi_notes", "suppliers", "method", "alignment",
                 "footer"],
}

PROMPT = """You are writing the short explanatory lines on a procurement
dashboard. A rule engine has already decided everything below; you are
captioning its output for a buyer.

Hard rules:
- Never state a number that is not in the facts below. Do no arithmetic.
- One or two sentences per note. Plain, specific, no filler, no headings.
- Name the rule behind a claim where there is one: mandatory compliance,
  MOQ feasibility, ceiling materiality, rank by cost, the promotion rule.
- A compliance requirement a quote does not mention is a gap, never "met".
- Do not invent a supplier's country, address, certification or any term that
  is not stated below.
- Write about these suppliers and figures only. No generic advice.

For "alignment", write one entry per category-strategy point that the facts
support - dual sourcing, ceiling prices, supplier diversity, compliance - with
a short title and one sentence of detail.

For "negotiation", write one entry per over-ceiling line in the facts, keyed by
its cas_no, saying how big the gap is and what to benchmark against.

For "footer": "covers" says what this comparison is; "comparable" says how
prices were put on a common basis; "double_check" names what a reader should
verify before acting.

Facts:
{facts}
"""


def _d(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def build_facts(result: dict) -> dict:
    """A compact, numbers-only digest of the run for the model to caption.

    Deliberately smaller than the run itself: the whole result is tens of
    kilobytes of per-line detail the notes do not need, and a shorter prompt is
    both cheaper and less likely to be paraphrased loosely.
    """
    kpis = result.get("kpis", {})
    suppliers = result.get("suppliers", [])
    gates = result.get("gates", {})

    cheapest = min((s for s in suppliers),
                   key=lambda s: _d(s["total_landed_cost_eur"]) or Decimal(0),
                   default=None)
    primary = next((s for s in suppliers if s.get("final_rank") == 1), None)

    gap_to_cheapest = None
    if primary and cheapest and cheapest["supplier_id"] != primary["supplier_id"]:
        low = _d(cheapest["total_landed_cost_eur"])
        high = _d(primary["total_landed_cost_eur"])
        if low:
            gap_to_cheapest = str(((high - low) / low * 100).quantize(Decimal("0.1")))

    return {
        "basket": {
            "materials": kpis.get("material_count"),
            "suppliers": kpis.get("supplier_count"),
            "ceiling_equivalent_total_eur": kpis.get("ceiling_equivalent_total_eur"),
            "spread_eur": kpis.get("spread_eur"),
            "spread_pct": kpis.get("spread_pct"),
            "gap_to_cheapest_pct": gap_to_cheapest,
        },
        "suppliers": [
            {
                "supplier_id": s["supplier_id"],
                "name": s["supplier_name"],
                "total_landed_cost_eur": s["total_landed_cost_eur"],
                "award_status": s.get("award_status"),
                "base_rank": s.get("base_rank"),
                "final_rank": s.get("final_rank"),
                "promoted_over": s.get("promoted_over"),
                "incoterm": s.get("incoterm"),
                "currency": s.get("currency"),
                "payment_terms_net_days": s.get("payment_terms_net_days"),
                "lead_time_weeks": f"{s.get('lead_time_min_weeks')}-"
                                   f"{s.get('lead_time_max_weeks')}",
                "discount_pct_applied": s.get("discount_pct_applied"),
                "vs_ceiling_pct": s.get("ceiling_equivalent_variance_pct"),
                "items_above_ceiling": sum(
                    1 for l in result.get("lines", [])
                    if l["supplier_id"] == s["supplier_id"] and l["above_ceiling"]),
                "advisory_gaps": s.get("advisory_gaps"),
                "failed_gate": s.get("failed_gate"),
                "engine_reason": s.get("primary_reason"),
            }
            for s in suppliers
        ],
        "gates": {
            sid: [{"gate": g["gate_name"], "passed": g["passed"],
                   "explanation": (g.get("detail") or {}).get("explanation")}
                  for g in rows]
            for sid, rows in gates.items()
        },
        "promotions": result.get("promotions", []),
        "allocation": result.get("allocation", []),
        "over_ceiling_lines": [
            {"cas_no": r["cas_no"], "material": r["material_name"],
             "supplier": r["supplier_name"], "gap_pct": r["gap_pct"],
             "ceiling_eur_l": r["ceiling_price_eur_l"],
             "landed_eur_l": r["landed_price_eur_l"],
             "annual_impact_eur": r["annual_impact_eur"]}
            for r in result.get("renegotiation", [])
        ],
        "cheapest_per_material": [
            {"material": l["material_name"], "cas_no": l["cas_no"],
             "supplier": l["supplier_name"],
             "landed_eur_l": l["landed_price_per_l_eur"]}
            for l in result.get("lines", []) if l.get("is_cheapest_for_material")
        ],
        "warnings": result.get("warnings", []),
    }


def _clean(payload) -> dict:
    """Keep only what the schema promised, as strings.

    The model is asked for prose; anything else it returns is discarded rather
    than rendered, so a stray number or object cannot reach the page.
    """
    if not isinstance(payload, dict):
        return {}

    def text(value) -> str:
        return value.strip() if isinstance(value, str) else ""

    def obj(key, fields) -> dict:
        source = payload.get(key)
        if not isinstance(source, dict):
            return {}
        return {f: text(source.get(f)) for f in fields}

    def rows(key, id_field) -> list:
        source = payload.get(key)
        if not isinstance(source, list):
            return []
        out = []
        for item in source:
            if isinstance(item, dict) and text(item.get(id_field)):
                out.append({id_field: text(item[id_field]),
                            "note": text(item.get("note")),
                            "title": text(item.get("title")),
                            "detail": text(item.get("detail"))})
        return out

    alignment = []
    if isinstance(payload.get("alignment"), list):
        for item in payload["alignment"]:
            if isinstance(item, dict) and text(item.get("title")):
                alignment.append({"title": text(item["title"]),
                                  "detail": text(item.get("detail"))})

    return {
        "headline": obj("headline", ("recommendation", "why")),
        "kpi_notes": obj("kpi_notes", ("gap", "target", "savings")),
        "method": obj("method", ("compliance", "target_price", "rank",
                                 "promotion")),
        "footer": obj("footer", ("covers", "comparable", "double_check")),
        "suppliers": rows("suppliers", "supplier_id"),
        "negotiation": rows("negotiation", "cas_no"),
        "alignment": alignment,
    }


def generate(result: dict) -> dict:
    """Ask the model for the notes. Returns {} rather than raising."""
    from .ingest.vertex import generate_json

    facts = json.dumps(build_facts(result), indent=1, default=str)
    try:
        raw = generate_json(PROMPT.format(facts=facts[:24000]), NOTES_SCHEMA)
    except Exception as exc:  # noqa: BLE001 - the dashboard works without notes
        log.warning("dashboard notes unavailable: %s: %s", type(exc).__name__, exc)
        return {}
    return _clean(raw)


def get_or_create(session: Session, run_id: str, refresh: bool = False) -> dict:
    """Stored notes for this run, generating them the first time.

    Always returns a dict. An older schema_version is still returned as-is -
    the dashboard picks the keys it recognises and leaves the rest blank, which
    is what lets a new dashboard ship over notes an old one wrote.
    """
    run = session.get(EvaluationRun, run_id)
    if not run:
        return {"available": False, "notes": EMPTY, "schema_version": SCHEMA_VERSION}

    row = session.get(RunNotes, run_id)
    if row and not refresh:
        return {"available": bool(row.notes), "notes": row.notes or EMPTY,
                "schema_version": row.schema_version, "model": row.model}

    notes = generate(run.result or {})
    if not notes:
        # Not stored: a transient Vertex failure should not freeze this run
        # into having no notes for ever.
        return {"available": False, "notes": EMPTY,
                "schema_version": SCHEMA_VERSION}

    if row:
        row.notes = notes
        row.schema_version = SCHEMA_VERSION
        row.model = settings.vertex_model
    else:
        session.add(RunNotes(run_id=run_id, notes=notes,
                             schema_version=SCHEMA_VERSION,
                             model=settings.vertex_model))
    session.commit()
    return {"available": True, "notes": notes,
            "schema_version": SCHEMA_VERSION, "model": settings.vertex_model}
