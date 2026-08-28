"""The sourcing agent, built on ADK.

It answers questions about a finished run and drafts the approval memo. Its
instruction is deliberately restrictive: it may only state numbers that came
back from a tool, and it must show the gate trail rather than assert a
conclusion.
"""
from __future__ import annotations

import logging

from .. import telemetry
from ..config import settings
from .tools import ALL_TOOLS

log = logging.getLogger(__name__)

APP_NAME = "sourcing-agent"

INSTRUCTION = """You are a procurement analyst explaining a completed supplier
quote evaluation to a buyer at a semiconductor plant.

The evaluation has already been calculated by a rule engine. Your job is to
explain it, never to redo it.

Hard rules:
- Never state a number that did not come back from a tool call. Do no
  arithmetic of your own, including percentages and differences.
- Always name the rule a claim rests on: Gate 1 mandatory compliance, Gate 2
  MOQ feasibility, Gate 3 ceiling materiality, base ranking by cost, or the
  promotion rule.
- When explaining a rank, show the trail: base rank by cost first, then whether
  the promotion rule fired and which of its four conditions held.
- The promotion rule only moves a supplier above a cheaper one when all four
  conditions hold: cost gap inside the band, the cheaper supplier has
  compliance gaps the candidate does not, payment terms equal or better, and
  lead time equal or faster. Report each one separately.
- A compliance requirement that a quote does not mention is a gap. Never
  describe it as met.
- Distinguish quoted figures from derived ones. Freight adjustments are a fixed
  percentage selected by Incoterm and currency conversions use a configured FX
  rate; neither is a figure the supplier quoted.
- Historical prices come from the buyer's own purchase history, and the share
  each vendor holds today is measured from it. The proposed award split is a
  configured assumption - say so when you use it.
- If a question asks what would happen under different assumptions, say that it
  needs a fresh evaluation. Do not simulate it.
- If the run does not contain the answer, say so plainly.

Write in plain prose for a buyer. Be concise and specific.
"""

MEMO_INSTRUCTION = """Write the sourcing approval package for management
sign-off, using only figures returned by your tools.

Output markdown, in exactly this order and with exactly these headings. Do not
add a section, drop one, or renumber them.

# SOURCING APPROVAL PACKAGE

## <category> — <date of the run>

Then a two-column table of: Category, Plant, Materials, Prepared by, Date,
Approver. Leave Prepared by and Approver blank for signature. There is no RFQ
reference field - the quotations do not carry one, so do not add a row for it.

## 1. Executive Summary
Three short paragraphs, each starting with its label: "Sourcing event summary:",
"Sourcing objective:", "Recommendation:".

## 2. Supplier Comparison
A table with the suppliers as columns and these as rows: Total Landed Cost,
Incoterm, Payment Terms, Lead Time, Quote Valid Until, Items Above Ceiling,
Compliance Gaps, Award Status.

## 3. Commercial Evaluation
Landed unit price per litre in EUR: one row per material, columns for the
ceiling price and each supplier. Follow it with one line on discount structures.

## 4. Benchmark Analysis
A table: Supplier, Total Landed Cost, vs. Historical Average, vs. Category
Ceiling (basket-equivalent). Mark which supplier is recommended and which is
cheapest.

## 5. Insights & Findings
Bullets only: the widest price spread, the largest single-item deviation, and
each concentration or lead-time risk.

## 6. Category Strategy Alignment
Bullets only: dual-sourcing policy, ceiling price alignment, supplier
diversity, compliance checklist.

## 7. Negotiation Opportunities
A table: Item, Supplier, Gap vs. Ceiling / Benchmark, Opportunity. Worst impact
first. Follow it with the total potential saving if the recommended supplier's
flagged items were repriced to ceiling.

## 8. Recommendation
A table of Rank, Supplier, Decision, then one Rationale paragraph.

## 9. Supporting Documents
Bullets: the source documents behind this run.

## 10. Approval Information
A blank two-column table of Approver, Approval Route, Status, Approval
Comments, where Status lists the three options as empty checkboxes. End with a
signature and date line.

Currency is EUR throughout and every amount says so. Do not invent a figure, an
RFQ number or a person's name; leave a field blank rather than filling it.
"""


def build_agent(instruction: str = INSTRUCTION):
    from google.adk.agents import Agent

    return Agent(
        name="sourcing_agent",
        model=settings.vertex_model,
        instruction=instruction,
        tools=ALL_TOOLS,
    )


async def _ask(instruction: str, prompt: str, operation: str, run_id: str) -> str:
    """Run the agent once and collect its text.

    The span opened here is the parent the ADK spans hang off. ADK instruments
    itself through the global tracer provider, so once telemetry.setup() has
    registered one, `invocation`, `invoke_agent sourcing_agent`, `call_llm` and
    `execute_tool <name>` appear underneath this span without ADK being
    configured for it. Nothing below has to pass a tracer around.
    """
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    with telemetry.tracer().start_as_current_span(f"agent.{operation}") as span:
        telemetry.set_attributes(
            span,
            **{
                "gen_ai.operation.name": operation,
                "gen_ai.agent.name": "sourcing_agent",
                "gen_ai.request.model": settings.vertex_model,
                "run.id": run_id,
            },
        )

        runner = InMemoryRunner(agent=build_agent(instruction), app_name=APP_NAME)
        session = await runner.session_service.create_session(
            app_name=APP_NAME, user_id="buyer")

        chunks: list[str] = []
        async for event in runner.run_async(
            user_id="buyer",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        chunks.append(part.text)

        answer = "".join(chunks).strip()
        telemetry.set_attributes(span, **{"gen_ai.response.length": len(answer)})
        return answer


def _agent_failed(operation: str, exc: Exception) -> None:
    """Record a fallback loudly enough to be findable.

    Falling back is the right behaviour - a buyer still gets an answer from the
    stored run - but it is also why an agent that never works can look like it
    is working. The span is marked failed so the trail shows the agent was
    skipped, and the traceback is logged rather than a one-line message,
    because "why are there no ADK spans" is otherwise unanswerable from the
    outside.
    """
    telemetry.record_exception(exc)
    log.exception("agent %s failed, falling back to the stored trail", operation)


async def explain(run_id: str, question: str) -> str:
    prompt = (
        f"The evaluation run id is {run_id}. Use your tools against that run id "
        f"to answer this question from the buyer:\n\n{question}"
    )
    try:
        return await _ask(INSTRUCTION, prompt, "explain", run_id)
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the request
        _agent_failed("explain", exc)
        return fallback_explanation(run_id)
    finally:
        # The agent's spans are the point of this exercise and Cloud Run can
        # take the CPU away as soon as the response is written, so they go out
        # now rather than on the batch processor's timer.
        telemetry.flush()


async def draft_memo(run_id: str) -> str:
    prompt = (
        f"The evaluation run id is {run_id}. Gather the full picture with your "
        f"tools and write the approval package summary."
    )
    try:
        return await _ask(MEMO_INSTRUCTION, prompt, "draft_memo", run_id)
    except Exception as exc:  # noqa: BLE001
        _agent_failed("draft_memo", exc)
        from ..render.memo import render_memo

        return render_memo(run_id)
    finally:
        telemetry.flush()


def fallback_explanation(run_id: str) -> str:
    """Used when Vertex is unreachable. Reads the same stored values the agent
    would have read, so the answer is still correct, just less fluent."""
    from .tools import get_gate_results, get_promotion_detail, get_run_summary

    summary = get_run_summary(run_id)
    if not summary or summary.get("error"):
        return "That run could not be found."

    lines = ["**Ranking**", ""]
    for supplier in summary["suppliers"]:
        rank = supplier.get("final_rank") or "-"
        lines.append(
            f"- Rank {rank}: {supplier['supplier_name']} - "
            f"EUR {supplier['total_landed_cost_eur']} - "
            f"{supplier.get('award_status')}. {supplier.get('primary_reason')}"
        )

    lines += ["", "**Gate trail**", ""]
    for supplier_id, gates in get_gate_results(run_id).items():
        for gate in gates:
            verdict = "pass" if gate["passed"] else "fail"
            lines.append(
                f"- {supplier_id} Gate {gate['gate_no']} "
                f"({gate['gate_name']}): {verdict}. "
                f"{gate['detail'].get('explanation', '')}"
            )

    promotions = get_promotion_detail(run_id)["promotions"]
    if promotions:
        lines += ["", "**Promotion rule**", ""]
        for promo in promotions:
            lines.append(
                f"- {promo['candidate_supplier_id']} against "
                f"{promo['cheaper_supplier_id']}: "
                f"cost {promo['cost_condition_met']}, "
                f"compliance {promo['compliance_condition_met']}, "
                f"payment terms {promo['payment_condition_met']}, "
                f"lead time {promo['lead_time_condition_met']} "
                f"-> {'promoted' if promo['promoted'] else 'not promoted'}"
            )

    return "\n".join(lines)
