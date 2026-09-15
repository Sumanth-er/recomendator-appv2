"""The sourcing agent, built on ADK.

One agent answers questions about a finished run, simulates what would change
under different assumptions, and changes the policy the engine runs on when the
buyer asks it to. The dashboard chat, the per-run ask endpoint and the A2A
surface all use it. Its instruction is deliberately restrictive: it may only
state numbers that came back from a tool, and it must show the gate trail
rather than assert a conclusion.

A second, read-only configuration drafts the approval memo.
"""
from __future__ import annotations

import asyncio
import logging
import os

from .. import telemetry
from ..config import settings
from .tools import AGENT_TOOLS, READ_TOOLS, TurnLog, record_turn

log = logging.getLogger(__name__)

APP_NAME = "sourcing-agent"
AGENT_NAME = "sourcing_agent"

DEFAULT_DESCRIPTION = (
    "A procurement analyst for a buyer at a semiconductor plant: explains a "
    "completed supplier quote evaluation, simulates what-if changes, and "
    "changes rule thresholds, ceiling prices, required volumes and compliance "
    "requirements when asked."
)

# How long the agent gets before the deterministic answer is used instead.
#
# An agent turn is several LLM round trips and a tool call each; when one of
# those stalls there is nothing to stop it, and the request sits open until
# nginx (300s) or Cloud Run cuts the connection - which reaches the browser as
# "Failed to fetch" with no answer at all. Falling back at a budget well inside
# those limits turns that into a correct, if less fluent, reply.
AGENT_TIMEOUT_SECONDS = float(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))

# No curly braces anywhere in this text: ADK reads a name in braces inside an
# instruction as a session-state placeholder.
INSTRUCTION = """You are the sourcing agent for a buyer at a semiconductor
plant. A deterministic rule engine has evaluated supplier quotations for a
basket of wet chemicals. You explain that evaluation, simulate what would
change under different assumptions, and change the policy it runs on when the
buyer tells you to.

Every request is one of three kinds. Decide which before calling any tool.

1. Explain - a question about the evaluation as it stands: ranking, gates,
   the promotion rule, compliance, prices, allocation, negotiation. Use the
   read tools against the run id. Change nothing.
2. What if - a hypothetical: "what if", "what would happen", "would X still
   win", "suppose", "how sensitive is". Call simulate_what_if. Nothing is
   saved.
3. Change - the buyer tells you to change something: "set", "change", "make",
   "lower", "raise", "remove", "add", "update", "apply that", "go ahead".
   Call apply_changes, or add_compliance_requirement for a requirement that is
   not on the checklist yet, or rerun_evaluation to re-evaluate with no
   change. This saves the change and creates a new evaluation run.

If you cannot tell whether the buyer wants a what-if or a real change, run the
simulation, report it, and ask whether to apply it. Never apply a change the
buyer did not ask for.

What can be simulated or changed. simulate_what_if and apply_changes take the
same arguments, lists of "KEY=VALUE" strings:
- policy_changes: rule thresholds, keyed as in get_current_policy, for example
  "ceiling_materiality_pct=10" (Gate 3), "moq_overbuy_threshold_pct=15"
  (Gate 2), "promotion_band_pct=12" (promotion rule) or
  "max_vendor_share_pct=70" (concentration check).
- ceiling_price_changes: a material's ceiling price in EUR per litre, keyed by
  CAS number from get_current_materials, for example "7664-93-9=0.90".
- volume_changes: a material's required volume in litres, keyed by CAS number,
  for example "7664-93-9=40000".
- compliance_changes: an existing checklist code's tier, keyed by code from
  get_current_compliance: "SDS_LANGUAGE=MANDATORY", "ISO_9001=ADVISORY" or
  "TSCA=REMOVED".
"Lower the ceiling for sulfuric acid" is a ceiling price; "tolerate more over
ceiling" is ceiling_materiality_pct; "make SDS mandatory" is a compliance tier.
If a request could mean more than one of these, ask which.

Values: a bare number is the new value. For a relative request pass the change
with a sign and let the tool resolve it: "ceiling_materiality_pct=+2" adds 2
percentage points, "7664-93-9=-10%" lowers that ceiling by ten percent of its
current value. For a percentage threshold, "by 2%" means 2 percentage points
unless the buyer says otherwise. Never work out a new value yourself. Look up
the current value first with get_current_policy, get_current_materials or
get_current_compliance unless this conversation already has it.

Reporting a what-if:
- Say plainly that it is a simulation and nothing was saved.
- State each change with its previous and new value, as the tool returned
  them.
- Say whether the recommendation changes and which suppliers' rank, award
  status or failed gate move, from the tool's outcome. If nothing moves, say
  so.
- If policy_in_force_matches_this_run is false, say the policy has changed
  since this run, so the comparison is against today's policy.
- Finish by giving the exact change in KEY=VALUE form and offering to apply
  it.

Reporting a change:
- Only say something was changed if the tool returned applied or added as
  true. If it returned errors, nothing was saved: say so and give the reason.
- State each change with its previous and new value, that a new evaluation run
  was created, and what moved in the outcome.
- Say that it applies to every future evaluation, and that runs already
  created keep the policy they were evaluated with.
- When the buyer says to apply a simulation, apply exactly the arguments that
  simulation recorded in this conversation.
- After a change, later questions are about the new run: use the new_run_id
  the tool returned as the run id.

Hard rules, always:
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
- Every run-scoped tool needs the evaluation run id. It comes with the request;
  if it does not, ask for it. Never invent one.
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

# Tools that change policy or create a run. A reply about one of these has to
# be accurate even when the model never got to write it - see _unfinished_reply.
CHANGING_TOOLS = {"apply_changes", "add_compliance_requirement", "rerun_evaluation"}


def build_agent(instruction: str = INSTRUCTION, description: str = "", tools=None):
    from google.adk.agents import Agent

    return Agent(
        name=AGENT_NAME,
        model=settings.vertex_model,
        instruction=instruction,
        description=description or DEFAULT_DESCRIPTION,
        tools=list(tools if tools is not None else AGENT_TOOLS),
    )


async def _ask(instruction: str, prompt: str, operation: str, run_id: str, tools=None) -> str:
    """Run the agent once and collect its reply.

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
                "gen_ai.agent.name": AGENT_NAME,
                "gen_ai.request.model": settings.vertex_model,
                "run.id": run_id,
            },
        )

        # A runner per call, closed when the call ends. InMemoryRunner builds a
        # session, artifact and memory service of its own each time, and the
        # session holds the whole conversation - this instruction, every tool
        # result, the model's replies. Without close() those services keep that
        # alive after the answer has been sent, which on a warm Cloud Run
        # instance accumulates until the container is killed mid-request and the
        # browser sees a dropped connection rather than an error.
        async with InMemoryRunner(agent=build_agent(instruction=instruction, tools=tools),
                                  app_name=APP_NAME) as runner:
            session = await runner.session_service.create_session(
                app_name=APP_NAME, user_id="buyer")

            final: list[str] = []
            interim: list[str] = []
            async for event in runner.run_async(
                user_id="buyer",
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text=prompt)]),
            ):
                if not (event.content and event.content.parts):
                    continue
                text = "".join(
                    part.text for part in event.content.parts
                    if getattr(part, "text", None) and not getattr(part, "thought", False)
                ).strip()
                if text:
                    # Text written alongside a tool call ("let me check the
                    # gates") is not the answer; the final response is. It is
                    # kept only in case the model ends without one.
                    (final if event.is_final_response() else interim).append(text)

            answer = "\n\n".join(final or interim).strip()

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


def _agent_timed_out(operation: str) -> None:
    """A stall, recorded as one - distinct from the agent refusing or erroring."""
    telemetry.record_exception(
        TimeoutError(f"agent {operation} exceeded {AGENT_TIMEOUT_SECONDS}s"))
    log.warning("agent %s exceeded its %.0fs budget; answering from the stored "
                "run instead", operation, AGENT_TIMEOUT_SECONDS)


def _transcript(history: list[dict] | None) -> str:
    """Earlier turns, replayed into the prompt.

    An agent turn that simulated or changed something carries its exact tool
    arguments, so "apply that" applies what was simulated rather than what the
    prose happened to say about it.
    """
    if not history:
        return ""
    turns = []
    for turn in history[-10:]:  # enough context without an unbounded prompt
        if turn["role"] == "user":
            turns.append(f"Buyer: {turn['content']}")
            continue
        turns.append(f"You: {turn['content']}")
        for action in turn.get("actions") or []:
            arguments = {k: v for k, v in (action.get("arguments") or {}).items() if v}
            created = f", which created run {action['new_run_id']}" if action.get("new_run_id") else ""
            turns.append(f"  (you called {action.get('tool')} with {arguments}{created})")
    return "Earlier in this conversation:\n" + "\n".join(turns) + "\n\n"


def _describe_action(action: dict) -> list[str]:
    tool = action.get("tool")
    if tool == "apply_changes":
        lines = []
        for change in action.get("changes") or []:
            subject = change["key"] + (f" ({change['material']})" if change.get("material") else "")
            unit = f" {change['unit']}" if change.get("unit") else ""
            lines.append(f"- Changed {change['kind']} {subject}: "
                         f"{change.get('previous')} to {change['new']}{unit}")
        return lines + [f"- Created evaluation run {action['new_run_id']}"]
    if tool == "add_compliance_requirement":
        added = action.get("arguments") or {}
        return [f"- Added compliance requirement {added.get('code')} "
                f"({added.get('label')}) as {added.get('tier')}"]
    if tool == "rerun_evaluation":
        return [f"- Re-evaluated the basket into run {action['new_run_id']}"]
    return []


def _unfinished_reply(run_id: str, turn: TurnLog) -> str:
    """The reply when the model could not write one.

    If a change was saved before the agent stalled, that has to be said -
    "nothing happened" would be wrong, and the buyer would ask for it again.
    Otherwise the stored run answers, and says nothing was changed.
    """
    done = [line for action in turn.actions if action.get("tool") in CHANGING_TOOLS
            for line in _describe_action(action)]
    if done:
        latest = turn.new_run_ids[-1] if turn.new_run_ids else run_id
        return "\n".join(
            ["The agent could not finish its reply, but these changes were saved:", ""]
            + done + ["", fallback_explanation(latest)])
    return ("The agent could not answer just now, and nothing was changed. "
            "Here is the stored evaluation instead.\n\n" + fallback_explanation(run_id))


async def chat(run_id: str, question: str, history: list[dict] | None = None,
               operation: str = "chat") -> dict:
    """Answer one request - explain, simulate or change; the agent decides.

    Returns the reply with what the turn did: new_run_id when the basket was
    re-evaluated into a new run, last_simulation (the arguments that would
    apply it) when the turn ended on an unsaved what-if, and the actions
    themselves for the transcript.

    Continuity is a transcript replayed into the prompt, not a persistent ADK
    session - _ask() still builds and tears down a fresh InMemoryRunner every
    call (see its docstring for why: a warm Cloud Run instance would
    otherwise accumulate open sessions across requests). The actual memory of
    the conversation lives in chat_message rows, passed in here as history,
    which is what makes it survive a request landing on a different Cloud Run
    instance than the one before it.
    """
    prompt = (
        f"The evaluation run id is {run_id}. Use it for every run-scoped tool, "
        f"unless a change in this turn creates a newer run.\n\n"
        f"{_transcript(history)}New request from the buyer:\n\n{question}"
    )
    with record_turn() as turn:
        try:
            answer = await asyncio.wait_for(
                _ask(INSTRUCTION, prompt, operation, run_id, tools=AGENT_TOOLS),
                timeout=AGENT_TIMEOUT_SECONDS)
            if not answer:
                log.warning("agent %s returned no text", operation)
                answer = _unfinished_reply(run_id, turn)
        except asyncio.TimeoutError:
            _agent_timed_out(operation)
            answer = _unfinished_reply(run_id, turn)
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail the request
            _agent_failed(operation, exc)
            answer = _unfinished_reply(run_id, turn)
        finally:
            # The agent's spans are the point of this exercise and Cloud Run can
            # take the CPU away as soon as the response is written, so they go
            # out now rather than on the batch processor's timer.
            telemetry.flush()

    ended_on_simulation = bool(turn.actions) and turn.actions[-1]["tool"] == "simulate_what_if"
    return {
        "answer": answer,
        "new_run_id": turn.new_run_ids[-1] if turn.new_run_ids else None,
        "last_simulation": turn.last_simulation if ended_on_simulation else None,
        "actions": turn.actions,
    }


async def explain(run_id: str, question: str) -> str:
    """One stateless request - the same agent as chat(), without a transcript."""
    return (await chat(run_id, question, operation="explain"))["answer"]


async def draft_memo(run_id: str) -> str:
    prompt = (
        f"The evaluation run id is {run_id}. Gather the full picture with your "
        f"tools and write the approval package summary."
    )
    try:
        return await asyncio.wait_for(
            _ask(MEMO_INSTRUCTION, prompt, "draft_memo", run_id, tools=READ_TOOLS),
            timeout=AGENT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _agent_timed_out("draft_memo")
        from ..render.memo import render_memo

        return render_memo(run_id)
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
