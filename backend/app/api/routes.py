"""HTTP API.

No authentication in this POC - the backend is deployed with public access and
the database password comes from a plain environment variable. That is a
deliberate scope decision, not an oversight; do not put real supplier data in a
deployment configured this way.
"""
from __future__ import annotations

from .. import telemetry

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, HTTPException, Response,
    UploadFile,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_session
from ..evaluation import EvaluationError, run_evaluation
from .. import reference_action as reference_actions
from ..ingest import storage
from ..ingest.pipeline import file_hash, process_document
from ..ingest.historical import load as load_historical, vendor_spend_summary
from ..ingest.strategy import apply_strategy, extract_strategy
from ..models import (
    ApprovalPackage, ApprovedSupplier, Benchmark, CategoryStrategy, ChatMessage,
    ChatSession, Comparison, ComplianceRequirement, Demand, EvaluationRun,
    FreightPolicy, HistoricalPrice, HistoricalPurchase, Material, PolicyConfig,
    Quote, RunNotes, SourceDocument, Supplier,
)

def _process_in_background(document_id: str, parent_ctx=None) -> None:
    """Extraction, outside the request that triggered it.

    The upload response has already gone back to the browser by the time this
    runs, so this work gets its own trace rather than a child span. The link
    back to the uploading request is what joins the two in Trace Explorer.

    The flush is not optional: Cloud Run throttles CPU once a response has been
    sent, which is exactly the window this task runs in, and a batch processor
    waiting on its five second timer would never get the CPU to send.
    """
    links = None
    try:
        from opentelemetry.trace import Link

        if parent_ctx is not None and parent_ctx.is_valid:
            links = [Link(parent_ctx)]
    except Exception:  # noqa: BLE001 - tracing must not break ingestion
        links = None

    try:
        with telemetry.tracer().start_as_current_span(
            "ingest.process_document",
            links=links,
            attributes={"document.id": document_id},
        ):
            try:
                with SessionLocal() as session:
                    process_document(session, document_id)
            except Exception as exc:  # noqa: BLE001
                telemetry.record_exception(exc)
                raise
    finally:
        telemetry.flush()

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Document AI online processing caps a request at 20 MB. Rejecting here gives a
# clear message instead of a failure three stages later.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class ComparisonCreate(BaseModel):
    name: str = "New comparison"


class AskRequest(BaseModel):
    question: str


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


@router.delete("/comparisons/{comparison_id}")
def delete_comparison(comparison_id: str, session: Session = Depends(get_session)):
    """Remove a comparison and everything that hangs off it.

    The ORM cascade covers documents, and through them quotes, quote lines and
    discounts. It does not cover chat sessions, run notes, evaluation runs or
    approval packages: those reference the comparison or its runs by id
    without a relationship, so they have to go explicitly and in this order.
    A chat message can point at a run it created and notes and packages point
    at their run, so on Postgres deleting the runs first is a foreign key
    violation.

    The source PDFs in Cloud Storage are deliberately left alone. Deleting a
    row is recoverable from a backup; deleting the supplier's original document
    is not, and nothing in this POC needs the bucket to stay tidy.
    """
    comparison = session.get(Comparison, comparison_id)
    if not comparison:
        raise HTTPException(404, "comparison not found")

    chat_ids = [
        c.session_id for c in session.scalars(
            select(ChatSession).where(ChatSession.comparison_id == comparison_id))
    ]
    if chat_ids:
        session.query(ChatMessage).filter(
            ChatMessage.session_id.in_(chat_ids)).delete(synchronize_session=False)
        session.query(ChatSession).filter(
            ChatSession.session_id.in_(chat_ids)).delete(synchronize_session=False)

    run_ids = [
        r.run_id for r in session.scalars(
            select(EvaluationRun).where(
                EvaluationRun.comparison_id == comparison_id))
    ]
    packages = 0
    if run_ids:
        session.query(RunNotes).filter(
            RunNotes.run_id.in_(run_ids)).delete(synchronize_session=False)
        packages = session.query(ApprovalPackage).filter(
            ApprovalPackage.run_id.in_(run_ids)).delete(synchronize_session=False)
    runs = session.query(EvaluationRun).filter(
        EvaluationRun.comparison_id == comparison_id).delete(
            synchronize_session=False)

    documents = len(comparison.documents)
    session.delete(comparison)
    session.commit()

    log.info("deleted comparison %s: %d documents, %d runs, %d packages",
             comparison_id, documents, runs, packages)
    return {
        "deleted": comparison_id,
        "documents": documents,
        "runs": runs,
        "packages": packages,
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

    # Captured here, while the request span is still current, so the
    # background task can link back to the upload that started it.
    ctx = telemetry.current_span_context()

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

        # background.add_task(_process_in_background, document.document_id)
        background.add_task(_process_in_background, document.document_id, ctx)
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

    # CAS is the match key, but a bare CAS number is not something a buyer
    # reads. The dashboard resolves the catalogue name through the engine;
    # this screen has to do its own lookup, or it shows 7664-93-9 where the
    # rest of the app says Sulfuric Acid 98%. A line whose CAS is outside the
    # basket resolves to None and is left as the CAS alone, which is the
    # honest answer - the validator already flags it.
    material_names = {
        m.cas_no: m.name for m in session.scalars(
            select(Material).where(
                Material.cas_no.in_([l.cas_no for l in quote.lines if l.cas_no])))
    } if quote.lines else {}

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
                # The catalogue name for the CAS, when it is a material we
                # buy. The supplier's own wording stays in its own field -
                # it is display only and never matched on.
                "material_name": material_names.get(line.cas_no),
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
    """Evaluate the basket under the policy in force - the Evaluate button.

    Never carries the agent's run-only changes: those belong to the run the
    agent made and to nothing else.
    """
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
        # Set only on a run the agent made under changed values; the dashboard
        # says so rather than letting it pass for the policy in force.
        "overrides": run.overrides,
        "result": run.result,
    }


# ---------------------------------------------------------------------------
# Approval package and the agent
# ---------------------------------------------------------------------------

@router.post("/runs/{run_id}/package")
async def create_package(
    run_id: str,
    use_agent: bool = False,
    session: Session = Depends(get_session),
):
    """Draft the approval package for this run.

    Deterministic by default. The package is a signed document of exact
    figures - ten numbered sections, seven tables - and the Word file that
    downloads is rendered from the same structure. Letting a model paraphrase
    it would put a different set of words on screen than in the file that gets
    approved, over numbers that have to agree with the dashboard.

    use_agent=true gives the model-written version instead, which reads better
    and is worth having when someone wants prose rather than a form. It is not
    the default because it cannot be guaranteed to match the download.
    """
    run = session.get(EvaluationRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")

    if use_agent:
        from ..agent.agent import draft_memo

        summary = await draft_memo(run_id)
    else:
        from ..render.memo import render_memo

        summary = render_memo(run_id)

    package = ApprovalPackage(run_id=run_id, summary_md=summary)
    session.add(package)
    session.commit()
    return {"package_id": package.package_id, "summary_md": summary,
            "source": "agent" if use_agent else "engine"}


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


@router.get("/runs/{run_id}/notes")
def get_run_notes(run_id: str, refresh: bool = False,
                  session: Session = Depends(get_session)):
    """Short written notes for the dashboard, drafted once per run.

    Never fails the page. A model that is unreachable, slow or that returns
    something unexpected comes back as available=false with empty notes, and
    the dashboard renders every number exactly as it would otherwise - the
    prose slots are simply blank.
    """
    if not session.get(EvaluationRun, run_id):
        raise HTTPException(404, "run not found")

    from ..notes import EMPTY, SCHEMA_VERSION, get_or_create

    try:
        return get_or_create(session, run_id, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 - notes are decoration, not data
        log.warning("dashboard notes failed for run %s: %s", run_id, exc)
        session.rollback()
        return {"available": False, "notes": EMPTY,
                "schema_version": SCHEMA_VERSION}


@router.get("/runs/{run_id}/package.docx")
def download_package(run_id: str, session: Session = Depends(get_session)):
    """The approval package as a Word document.

    Rendered from the stored run rather than from the agent's prose, so the
    file that gets signed carries the same figures the dashboard shows. The
    filename is dated because these get mailed around and filed.
    """
    run = session.get(EvaluationRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")

    try:
        from ..render.docx import render_docx

        content = render_docx(run_id)
    except ImportError as exc:  # python-docx missing from the image
        raise HTTPException(
            501, "Word export is unavailable: python-docx is not installed"
        ) from exc

    if content is None:
        raise HTTPException(404, "run not found")

    stamp = run.created_at.strftime("%Y-%m-%d") if run.created_at else "undated"
    filename = f"Sourcing_Approval_Package_{stamp}.docx"
    return Response(
        content=content,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/runs/{run_id}/ask")
async def ask_agent(run_id: str, payload: AskRequest,
                    session: Session = Depends(get_session)):
    """One stateless request to the agent - no transcript is kept.

    The same agent as the dashboard chat, so a request to change policy is
    acted on here too; new_run_id says when that created a run.
    """
    if not session.get(EvaluationRun, run_id):
        raise HTTPException(404, "run not found")

    from ..agent.agent import chat as agent_chat

    reply = await agent_chat(run_id, payload.question, operation="explain")
    return {"answer": reply["answer"], "new_run_id": reply["new_run_id"],
            "last_simulation": reply["last_simulation"]}

# ---------------------------------------------------------------------------
# Chat sessions - a multi-turn conversation scoped to a basket, persisted so
# it survives landing on a different Cloud Run instance between messages.
# ---------------------------------------------------------------------------

@router.post("/comparisons/{comparison_id}/chat/sessions")
def create_chat_session(comparison_id: str, session: Session = Depends(get_session)):
    if not session.get(Comparison, comparison_id):
        raise HTTPException(404, "comparison not found")

    chat_session = ChatSession(comparison_id=comparison_id)
    session.add(chat_session)
    session.commit()
    return {"session_id": chat_session.session_id}


@router.get("/chat/sessions/{session_id}/messages")
def get_chat_messages(session_id: str, session: Session = Depends(get_session)):
    chat_session = session.get(ChatSession, session_id)
    if not chat_session:
        raise HTTPException(404, "chat session not found")

    messages = session.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.message_id)
    )
    return {
        "comparison_id": chat_session.comparison_id,
        "messages": [
            {"role": m.role, "content": m.content, "resulting_run_id": m.resulting_run_id,
             "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in messages
        ],
    }


@router.post("/chat/sessions/{session_id}/ask")
async def ask_chat_session(session_id: str, payload: AskRequest,
                           run_id: str, session: Session = Depends(get_session)):
    """Ask the agent inside a session: explain, simulate, or change policy.

    run_id anchors which run's basket the tools operate on; it can be a
    different run each call within the same session - a change the agent
    makes creates a new run, and the conversation carries on about that one.

    The reply carries new_run_id when this turn changed policy and
    re-evaluated, and last_simulation - the exact arguments that would apply
    it - when the turn ended on an unsaved what-if.
    """
    chat_session = session.get(ChatSession, session_id)
    if not chat_session:
        raise HTTPException(404, "chat session not found")
    run = session.get(EvaluationRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run.comparison_id != chat_session.comparison_id:
        raise HTTPException(400, "that run belongs to a different comparison "
                                 "than this chat session")
    if not payload.question.strip():
        raise HTTPException(400, "the question is empty")

    history = [
        {"role": m.role, "content": m.content, "actions": m.actions}
        for m in session.scalars(
            select(ChatMessage).where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.message_id)
        )
    ]

    session.add(ChatMessage(session_id=session_id, role="user", content=payload.question))
    chat_session.last_active_at = datetime.now(timezone.utc)
    session.commit()

    from ..agent.agent import chat as agent_chat

    reply = await agent_chat(run_id, payload.question, history)

    session.add(ChatMessage(
        session_id=session_id, role="agent", content=reply["answer"],
        resulting_run_id=reply["new_run_id"], actions=reply["actions"] or None,
    ))
    session.commit()

    return {
        "answer": reply["answer"],
        "run_id": reply["new_run_id"] or run_id,
        "new_run_id": reply["new_run_id"],
        "last_simulation": reply["last_simulation"],
    }


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
# Rule thresholds and constants - the policy_config table
# ---------------------------------------------------------------------------

class PolicyValueUpdate(BaseModel):
    key: str
    value: Decimal


class PolicyBulkUpdate(BaseModel):
    updates: list[PolicyValueUpdate]


@router.put("/reference/policy")
def update_policy(payload: PolicyBulkUpdate, session: Session = Depends(get_session)):
    """Edit rule thresholds and constants from the Policy in force screen.

    Every evaluation run snapshots the policy in force at the time it ran
    (EvaluationRun.policy_snapshot), so a change here never rewrites an
    earlier run's stored figures - it only takes effect on the next
    evaluation. This screen is the only place the policy in force changes:
    the agent can evaluate a basket under different values, but only into a
    run of its own. The validation itself lives in reference_actions, shared
    with the agent's run-only changes - one set of rules, not two that can
    drift apart.
    """
    result = reference_actions.apply_policy_updates(
        session, [u.model_dump() for u in payload.updates])
    if result["updated"]:
        log.info("policy_config updated: %s",
                 ", ".join(f"{u['key']}={u['value']}" for u in result["updated"]))
    return result

# ---------------------------------------------------------------------------
# Compliance checklist - the compliance_requirement table
# ---------------------------------------------------------------------------

class ComplianceUpdate(BaseModel):
    code: str
    label: str | None = None
    tier: str | None = None          # MANDATORY | ADVISORY
    match_hint: str | None = None


class ComplianceBulkUpdate(BaseModel):
    updates: list[ComplianceUpdate]


@router.put("/reference/compliance")
def update_compliance(payload: ComplianceBulkUpdate, session: Session = Depends(get_session)):
    """Add or edit compliance checklist items from the Policy in force screen.

    Tier decides which gate a code drives: MANDATORY feeds Gate 1 (exclusion),
    ADVISORY feeds the promotion rule's compliance condition - flattening the
    two breaks the promotion rule. Extraction re-reads this table on every
    document processed, so a tier flip or a brand-new code takes effect on
    the next upload, not retroactively. The validation itself lives in
    reference_actions, shared with the agent's run-only changes.
    """
    result = reference_actions.apply_compliance_updates(
        session, [u.model_dump() for u in payload.updates])
    if result["updated"] or result["created"]:
        log.info("compliance_requirement changed: updated=%s created=%s",
                 [u["code"] for u in result["updated"]],
                 [c["code"] for c in result["created"]])
    return result


@router.delete("/reference/compliance/{code}")
def delete_compliance(code: str, session: Session = Depends(get_session)):
    """Remove a compliance checklist item from the Policy in force screen.

    A tombstone is written so seed.py's reconciliation loop never resurrects
    one of the 7 seeded defaults on the next cold start. Editing or
    re-adding the same code later, via PUT /reference/compliance, clears the
    tombstone automatically.
    """
    result = reference_actions.remove_compliance(session, code)
    if "error" in result:
        raise HTTPException(404, result["error"])
    log.info("compliance_requirement deleted: %s", result["deleted"])
    return result
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
            {"code": c.code, "label": c.label, "tier": c.tier,
             "manual_override": c.manual_override}
            for c in session.scalars(select(ComplianceRequirement))
        ],
        "policy": [
            {"key": p.key, "value": float(p.value), "unit": p.unit,
             "description": p.description, "section_ref": p.section_ref}
            for p in session.scalars(select(PolicyConfig))
        ],
    }
