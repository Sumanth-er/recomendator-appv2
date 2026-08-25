"""HTTP API.

No authentication in this POC - the backend is deployed with public access and
the database password comes from a plain environment variable. That is a
deliberate scope decision, not an oversight; do not put real supplier data in a
deployment configured this way.
"""
from __future__ import annotations

import logging

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_session
from ..evaluation import EvaluationError, run_evaluation
from ..ingest import storage
from ..ingest.pipeline import file_hash, process_document
from ..ingest.historical import load as load_historical, vendor_spend_summary
from ..ingest.strategy import apply_strategy, extract_strategy
from ..models import (
    ApprovalPackage, ApprovedSupplier, Benchmark, CategoryStrategy, Comparison,
    ComplianceRequirement, Demand, EvaluationRun, FreightPolicy,
    HistoricalPrice, HistoricalPurchase, Material, PolicyConfig, Quote,
    SourceDocument, Supplier,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Document AI online processing caps a request at 20 MB. Rejecting here gives a
# clear message instead of a failure three stages later.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class ComparisonCreate(BaseModel):
    name: str = "New comparison"


class AskRequest(BaseModel):
    question: str


def _process_in_background(document_id: str) -> None:
    """Runs outside the request so the browser is not held open through
    Document AI and Gemini."""
    with SessionLocal() as session:
        process_document(session, document_id)


# ---------------------------------------------------------------------------
# Comparisons - a batch of quotes evaluated together
# ---------------------------------------------------------------------------

@router.post("/comparisons")
def create_comparison(payload: ComparisonCreate, session: Session = Depends(get_session)):
    comparison = Comparison(name=payload.name)
    session.add(comparison)
    session.commit()
    return {"comparison_id": comparison.comparison_id, "name": comparison.name}


@router.get("/comparisons")
def list_comparisons(session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Comparison).order_by(Comparison.created_at.desc())).all()
    return [
        {
            "comparison_id": c.comparison_id,
            "name": c.name,
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "document_count": len(c.documents),
        }
        for c in rows
    ]


@router.get("/comparisons/{comparison_id}")
def get_comparison(comparison_id: str, session: Session = Depends(get_session)):
    comparison = session.get(Comparison, comparison_id)
    if not comparison:
        raise HTTPException(404, "comparison not found")

    documents = []
    supplier_counts: dict[str, int] = {}
    for document in comparison.documents:
        quote = document.quote
        supplier_name = None
        if quote:
            supplier = session.get(Supplier, quote.supplier_id)
            supplier_name = supplier.short_name if supplier else quote.supplier_id
            supplier_counts[quote.supplier_id] = supplier_counts.get(
                quote.supplier_id, 0) + 1
        documents.append({
            "document_id": document.document_id,
            "filename": document.original_filename,
            "status": document.status,
            "error_detail": document.error_detail,
            "page_count": document.page_count,
            "supplier_name": supplier_name,
            "quote_id": quote.quote_id if quote else None,
            "source_url": storage.signed_url(document.gcs_uri or ""),
        })

    duplicates = [s for s, count in supplier_counts.items() if count > 1]
    ready = bool(documents) and all(d["status"] == "READY" for d in documents)

    runs = session.scalars(
        select(EvaluationRun)
        .where(EvaluationRun.comparison_id == comparison_id)
        .order_by(EvaluationRun.created_at.desc())
    ).all()

    return {
        "comparison_id": comparison.comparison_id,
        "name": comparison.name,
        "status": comparison.status,
        "documents": documents,
        "can_evaluate": ready and not duplicates,
        "duplicate_suppliers": duplicates,
        "runs": [
            {"run_id": r.run_id,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in runs
        ],
    }


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@router.post("/comparisons/{comparison_id}/documents")
async def upload_documents(
    comparison_id: str,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
):
    comparison = session.get(Comparison, comparison_id)
    if not comparison:
        raise HTTPException(404, "comparison not found")

    accepted, duplicates, rejected = [], [], []
    for upload in files:
        content = await upload.read()
        if not content:
            continue
        if len(content) > MAX_UPLOAD_BYTES:
            rejected.append({
                "filename": upload.filename,
                "reason": (
                    f"{len(content) / 1_048_576:.1f} MB exceeds the 20 MB limit "
                    "for Document AI online processing"
                ),
            })
            continue
        digest = file_hash(content)

        # The same file twice in one comparison is an accident, absorbed
        # quietly. The same file in a different comparison is legitimate.
        existing = session.scalar(
            select(SourceDocument).where(
                SourceDocument.comparison_id == comparison_id,
                SourceDocument.content_sha256 == digest,
            )
        )
        if existing:
            duplicates.append(upload.filename)
            continue

        document = SourceDocument(
            comparison_id=comparison_id,
            original_filename=upload.filename or "quote.pdf",
            content_sha256=digest,
        )
        session.add(document)
        session.flush()

        document.gcs_uri = storage.upload_bytes(
            f"quotes/{comparison_id}/{document.document_id}.pdf",
            content,
            upload.content_type or "application/pdf",
        )
        session.commit()

        background.add_task(_process_in_background, document.document_id)
        accepted.append({
            "document_id": document.document_id,
            "filename": document.original_filename,
        })

    return {"accepted": accepted, "duplicates_ignored": duplicates,
            "rejected": rejected}


@router.post("/documents/{document_id}/reprocess")
def reprocess_document(
    document_id: str,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
):
    document = session.get(SourceDocument, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    document.status = "UPLOADED"
    document.error_detail = None
    session.commit()
    background.add_task(_process_in_background, document_id)
    return {"status": "queued"}


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, session: Session = Depends(get_session)):
    document = session.get(SourceDocument, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    session.delete(document)
    session.commit()
    return {"deleted": document_id}


@router.get("/quotes/{quote_id}")
def get_quote(quote_id: str, session: Session = Depends(get_session)):
    """Everything extracted from one quote, with confidence and page numbers."""
    quote = session.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "quote not found")
    supplier = session.get(Supplier, quote.supplier_id)

    return {
        "quote_id": quote.quote_id,
        "supplier_name": supplier.short_name if supplier else quote.supplier_id,
        "quote_no": quote.quote_no,
        "quote_date": quote.quote_date.isoformat() if quote.quote_date else None,
        "valid_until": quote.valid_until.isoformat() if quote.valid_until else None,
        "currency": quote.currency,
        "incoterm": quote.incoterm,
        "incoterm_location": quote.incoterm_location,
        "payment_terms_net_days": quote.payment_terms_net_days,
        "lead_time_min_weeks": (
            float(quote.lead_time_min_weeks) if quote.lead_time_min_weeks else None),
        "lead_time_max_weeks": (
            float(quote.lead_time_max_weeks) if quote.lead_time_max_weeks else None),
        "compliance": quote.compliance,
        "provenance": quote.provenance,
        "source_url": storage.signed_url(quote.document.gcs_uri or ""),
        "discounts": [
            {"discount_pct": float(d.discount_pct),
             "condition_type": d.condition_type,
             "condition_text": d.condition_text}
            for d in quote.discounts
        ],
        "lines": [
            {
                "line_no": line.line_no,
                "cas_no": line.cas_no,
                "supplier_description": line.supplier_description,
                "quantity": float(line.quantity) if line.quantity else None,
                "uom": line.uom,
                "unit_price": float(line.unit_price) if line.unit_price else None,
                "currency": line.currency,
                "line_total_stated": (
                    float(line.line_total_stated) if line.line_total_stated else None),
                "moq_qty": float(line.moq_qty) if line.moq_qty else None,
                "moq_uom": line.moq_uom,
                "moq_text": line.moq_text,
                "flags": line.flags or [],
                "provenance": line.provenance or {},
            }
            for line in sorted(quote.lines, key=lambda l: l.line_no)
        ],
    }


# ---------------------------------------------------------------------------
# Evaluation runs
# ---------------------------------------------------------------------------

@router.post("/comparisons/{comparison_id}/runs")
def create_run(comparison_id: str, session: Session = Depends(get_session)):
    try:
        run = run_evaluation(session, comparison_id)
    except EvaluationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"run_id": run.run_id}


@router.get("/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(get_session)):
    run = session.get(EvaluationRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return {
        "run_id": run.run_id,
        "comparison_id": run.comparison_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "engine_version": run.engine_version,
        "policy_snapshot": run.policy_snapshot,
        "result": run.result,
    }


# ---------------------------------------------------------------------------
# Approval package and the agent
# ---------------------------------------------------------------------------

@router.post("/runs/{run_id}/package")
async def create_package(run_id: str, session: Session = Depends(get_session)):
    run = session.get(EvaluationRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")

    from ..agent.agent import draft_memo

    summary = await draft_memo(run_id)
    package = ApprovalPackage(run_id=run_id, summary_md=summary)
    session.add(package)
    session.commit()
    return {"package_id": package.package_id, "summary_md": summary}


@router.get("/runs/{run_id}/package")
def get_package(run_id: str, session: Session = Depends(get_session)):
    package = session.scalar(
        select(ApprovalPackage)
        .where(ApprovalPackage.run_id == run_id)
        .order_by(ApprovalPackage.created_at.desc())
    )
    if not package:
        raise HTTPException(404, "no package for this run yet")
    return {
        "package_id": package.package_id,
        "summary_md": package.summary_md,
        "status": package.status,
    }


@router.post("/runs/{run_id}/ask")
async def ask_agent(run_id: str, payload: AskRequest,
                    session: Session = Depends(get_session)):
    if not session.get(EvaluationRun, run_id):
        raise HTTPException(404, "run not found")

    from ..agent.agent import explain

    return {"answer": await explain(run_id, payload.question)}


# ---------------------------------------------------------------------------
# Category strategy document - one-time policy setup (spec 2.3)
# ---------------------------------------------------------------------------

@router.post("/reference/strategy")
async def upload_strategy(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Read a category strategy document and apply the policy it states.

    Only what the document actually states is changed; anything it is silent
    about keeps its current value, so this never blanks a threshold by
    omission.
    """
    content = await file.read()
    if not content:
        raise HTTPException(400, "the uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "file exceeds the 20 MB limit")

    filename = file.filename or "category_strategy"
    try:
        extracted = extract_strategy(content, filename, file.content_type)
    except Exception as exc:  # noqa: BLE001 - reported to the user
        log.exception("category strategy extraction failed")
        raise HTTPException(422, f"could not read the document: {exc}") from exc

    gcs_uri = None
    if settings.bucket:
        gcs_uri = storage.upload_bytes(
            f"strategy/{filename}", content,
            file.content_type or "application/octet-stream")

    return apply_strategy(session, extracted, filename, gcs_uri)


@router.get("/reference/strategy")
def get_strategy(session: Session = Depends(get_session)):
    """The strategy document in force, if one has been uploaded."""
    strategy = session.scalar(
        select(CategoryStrategy)
        .where(CategoryStrategy.is_active.is_(True))
        .order_by(CategoryStrategy.uploaded_at.desc())
    )
    if not strategy:
        return {"active": None, "using_defaults": True}
    return {
        "active": {
            "strategy_id": strategy.strategy_id,
            "category": strategy.category,
            "version": strategy.version,
            "effective_date": (
                strategy.effective_date.isoformat()
                if strategy.effective_date else None),
            "source_filename": strategy.source_filename,
            "uploaded_at": (
                strategy.uploaded_at.isoformat() if strategy.uploaded_at else None),
            "applied_summary": strategy.applied_summary,
        },
        "using_defaults": False,
    }


# ---------------------------------------------------------------------------
# Historical purchase prices - the SAP BW-style extract (spec 2.2)
# ---------------------------------------------------------------------------

@router.post("/reference/historical")
async def upload_historical(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Load the SAP BW extract.

    An .xlsx with both sheets, or a single-sheet CSV of either shape. The price
    summary gives the benchmark for section 4.2; the PO lines give the vendor
    spend that the concentration check would otherwise have to assume.
    """
    content = await file.read()
    if not content:
        raise HTTPException(400, "the uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "file exceeds the 20 MB limit")
    try:
        return load_historical(session, content, file.filename or "historical.xlsx")
    except Exception as exc:  # noqa: BLE001 - reported to the user
        log.exception("historical extract failed")
        raise HTTPException(422, f"could not read the extract: {exc}") from exc


@router.delete("/reference/historical")
def clear_historical(session: Session = Depends(get_session)):
    prices = session.query(HistoricalPrice).delete()
    lines = session.query(HistoricalPurchase).delete()
    session.commit()
    return {"removed_summary_rows": prices, "removed_po_lines": lines}


# ---------------------------------------------------------------------------
# Reference data, shown in the dashboard so the policy in force is visible
# ---------------------------------------------------------------------------

@router.get("/reference")
def get_reference(session: Session = Depends(get_session)):
    return {
        "materials": [
            {"cas_no": m.cas_no, "name": m.name,
             "density_kg_per_l": float(m.density_kg_per_l) if m.density_kg_per_l else None}
            for m in session.scalars(select(Material))
        ],
        "benchmarks": [
            {"cas_no": b.cas_no,
             "ceiling_price_eur_l": float(b.ceiling_price_eur_l),
             "target_price_eur_l": (
                 float(b.target_price_eur_l) if b.target_price_eur_l else None)}
            for b in session.scalars(select(Benchmark))
        ],
        "demand": [
            {"cas_no": d.cas_no, "required_qty_l": float(d.required_qty_l),
             "plant": d.plant}
            for d in session.scalars(select(Demand))
        ],
        "freight_policy": [
            {"incoterm": f.incoterm, "freight_adj_pct": float(f.freight_adj_pct),
             "basis_note": f.basis_note, "is_estimate": f.is_estimate}
            for f in session.scalars(select(FreightPolicy))
        ],
        "historical": [
            {"cas_no": h.cas_no,
             "material_number": h.material_number,
             "avg_price_eur_l": float(h.avg_price_eur_l) if h.avg_price_eur_l else None,
             "min_price_eur_l": float(h.min_price_eur_l) if h.min_price_eur_l else None,
             "max_price_eur_l": float(h.max_price_eur_l) if h.max_price_eur_l else None,
             "last_invoiced_price_eur_l": (
                 float(h.last_invoiced_price_eur_l)
                 if h.last_invoiced_price_eur_l else None),
             "last_invoiced_date": (
                 h.last_invoiced_date.isoformat() if h.last_invoiced_date else None),
             "po_line_count": h.po_line_count,
             "period_from": h.period_from.isoformat() if h.period_from else None,
             "period_to": h.period_to.isoformat() if h.period_to else None,
             "source_filename": h.source_filename}
            for h in session.scalars(select(HistoricalPrice))
        ],
        "vendor_spend": vendor_spend_summary(session),
        "approved_suppliers": [
            {"supplier_key": a.supplier_key, "legal_name": a.legal_name}
            for a in session.scalars(select(ApprovedSupplier))
        ],
        "compliance_requirements": [
            {"code": c.code, "label": c.label, "tier": c.tier}
            for c in session.scalars(select(ComplianceRequirement))
        ],
        "policy": [
            {"key": p.key, "value": float(p.value), "unit": p.unit,
             "description": p.description, "section_ref": p.section_ref}
            for p in session.scalars(select(PolicyConfig))
        ],
    }
