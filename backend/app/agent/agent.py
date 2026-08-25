"""The sourcing agent, built on ADK.

It answers questions about a finished run and drafts the approval memo. Its
instruction is deliberately restrictive: it may only state numbers that came
back from a tool, and it must show the gate trail rather than assert a
conclusion.
"""
from __future__ import annotations

import logging

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

MEMO_INSTRUCTION = """Write an approval package summary for management sign-off,
using only figures returned by your tools.

Structure it as markdown with these sections:

## Recommendation
One short paragraph: which supplier is recommended as primary, which as
secondary, and the single most important reason.

## Commercial summary
A markdown table of every supplier: total landed cost, payment terms, lead time,
award status.

## How the ranking was reached
The trail in order: Gate 1, Gate 2, Gate 3, base rank by cost, then the
promotion rule with each of its four conditions and whether it held.

## Compliance position
Mandatory requirements per supplier, then advisory gaps. Quote the supporting
sentence where a tool returned one.

## Against historical prices
Each supplier's landed price against the historical average per material, where
history is available.

## Dual sourcing
What each vendor holds of historical spend today against the proposed share,
and whether that sits inside the concentration threshold.

## Negotiation priorities
The line items above the category strategy ceiling, worst impact first.

## Assumptions and caveats
Freight adjustments, the FX rate, any data quality flags, and anything the
engine warned about.

Do not add a section that is not listed. Do not invent figures.
"""


def build_agent(instruction: str = INSTRUCTION):
    from google.adk.agents import Agent

    return Agent(
        name="sourcing_agent",
        model=settings.vertex_model,
        instruction=instruction,
        tools=ALL_TOOLS,
    )


async def _ask(instruction: str, prompt: str) -> str:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

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
    return "".join(chunks).strip()


async def explain(run_id: str, question: str) -> str:
    prompt = (
        f"The evaluation run id is {run_id}. Use your tools against that run id "
        f"to answer this question from the buyer:\n\n{question}"
    )
    try:
        return await _ask(INSTRUCTION, prompt)
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the request
        log.warning("agent unavailable, using the stored trail instead: %s", exc)
        return fallback_explanation(run_id)


async def draft_memo(run_id: str) -> str:
    prompt = (
        f"The evaluation run id is {run_id}. Gather the full picture with your "
        f"tools and write the approval package summary."
    )
    try:
        return await _ask(MEMO_INSTRUCTION, prompt)
    except Exception as exc:  # noqa: BLE001
        log.warning("agent unavailable, rendering the memo from the run: %s", exc)
        from ..render.memo import render_memo

        return render_memo(run_id)


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
