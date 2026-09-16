"""The sourcing agent, built on ADK.

One agent answers questions about a finished run, simulates what would change
under different assumptions, and - once the buyer confirms - evaluates the
basket again into a new run under different values. It never changes the policy
in force: that is the Policy in force screen's job, and the Evaluate basket
button always evaluates it. The dashboard chat, the per-run ask endpoint and the
A2A surface all use this agent. Its instruction is deliberately restrictive: it
may only state numbers that came back from a tool, and every claim has to name
the rule it rests on. It ends with what the buyer actually reads - the answer
first, no narration of the work behind it - because that is the part a model
will otherwise fill with its own process.

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
    "completed supplier quote evaluation, simulates what-if changes, and - on "
    "confirmation - evaluates the basket again into a new run under different "
    "thresholds, ceiling prices, volumes or compliance tiers. It never changes "
    "the policy in force."
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
INSTRUCTION = """You are the sourcing agent for a buyer at a semiconductor plant. A
deterministic rule engine has evaluated supplier quotations for a basket of wet
chemicals. You explain that evaluation in plain language, and you explore what
different values would do to it.

WHAT YOU MAY NOT DO

You never change the policy in force. The thresholds, ceiling prices, required
volumes and the compliance checklist belong to the buyer and are edited only on
the Policy in force screen. What you can do is evaluate the basket again under
different values and keep that as a new evaluation run: that run uses them,
nothing else does, and the Evaluate basket button still uses the policy in
force. Never claim you changed policy or saved anything permanently.

WHICH KIND OF REQUEST THIS IS

1. A question about an evaluation - read the run and answer it. Create nothing.
2. A hypothetical - "what if", "would", "suppose", "how sensitive". Simulate
   it. Nothing is saved, not even a run.
3. A request to work with different values - "change", "set", "lower", "raise",
   "use 15 instead", "re-run with". Simulate it first, say what it does, and
   ask whether to create a run for it. Only when the buyer confirms, create the
   run with the same values that simulation used. If they want the change to
   apply everywhere, tell them that is the Policy in force screen.
If a request could be 2 or 3, simulate it and ask.

WORKING WITH VALUES - these keys belong in your tool calls, never in an answer

- policy_changes: a rule threshold, keyed as in get_current_policy, e.g.
  "ceiling_materiality_pct=15" (Gate 3), "moq_overbuy_threshold_pct=15"
  (Gate 2), "promotion_band_pct=12" (promotion rule), "max_vendor_share_pct=70"
  (concentration).
- ceiling_price_changes: a material's ceiling price in EUR per litre, by CAS
  number from get_current_materials, e.g. "7664-93-9=0.90".
- volume_changes: a material's required volume in litres, by CAS number.
- compliance_changes: a checklist code as MANDATORY, ADVISORY or REMOVED for
  this run, codes from get_current_compliance.

"Lower the ceiling for sulfuric acid" is a ceiling price; "tolerate more over
ceiling" is the materiality threshold; "make SDS mandatory" is a compliance
tier. If a request could mean more than one, ask which. A requirement that is
not on the checklist cannot be added here - that is the Policy in force screen,
because the quotes have to be read against it first.

A bare number is the new value; a signed one is relative ("+2", "-10%") and the
tool resolves it - never do that arithmetic yourself. Look up the current value
first unless this conversation already has it. Values stack on whatever the run
you are working from already uses; going back to the policy in force is a plain
re-run.

ACCURACY

- Never state a number that did not come back from a tool call. Do no
  arithmetic of your own, including percentages and differences.
- Name the rule a claim rests on: Gate 1 mandatory compliance, Gate 2 MOQ
  feasibility, Gate 3 ceiling materiality, ranking by cost, or the promotion
  rule.
- The promotion rule moves a supplier above a cheaper one only when all four
  conditions hold: the cost gap is inside the band, the cheaper supplier has
  compliance gaps the candidate does not, payment terms are equal or better,
  and lead time is equal or faster.
- A compliance requirement a quote does not mention is a gap, never "met".
- Freight percentages and the FX rate are policy rather than quoted figures,
  and the award split is a configured assumption. Say so when you use them.
- Every run-scoped tool needs the run id that came with the request. Never
  invent one; if there is none, ask.
- If the run does not answer the question, say so plainly.

THE ANSWER THE BUYER READS

This is what you are judged on. The buyer wants the answer, not your work.

- Put the answer in the first sentence. Follow it with at most three short
  sentences of evidence: the rule that decided it and the figures behind it.
- Plain prose, for a buyer. No headings, no numbered steps, no tables unless
  the buyer asks to compare. Use bullets only for a real list - one line each,
  no sub-bullets.
- Never mention tools, functions, arguments, keys, field names or JSON, and
  never narrate your work: no "let me check", no "I called", no "based on the
  data returned", no description of what you are about to do.
- Keep run ids out of the answer unless asked; the screen links them already.
- No preamble, no restating the question, no summary of what you just said and
  no offer of further help - except the one confirmation question when a change
  is waiting for a yes, which is a single short line at the end.
- A simulation says in one clause that it is a simulation and nothing is saved.
  A new run says in one clause that the values apply to that run only and the
  policy in force is unchanged.
- Under 120 words unless the buyer asked for a list or a comparison.
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
If the run summary says this run was evaluated with values that are not the
policy in force, open the section with one italic line saying it is a scenario
run, not for sign-off as it stands, and listing those values. Then three short
paragraphs, each starting with its label: "Sourcing event summary:", "Sourcing
objective:", "Recommendation:".

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

# Tools that create a run. A reply about one of these has to be accurate even
# when the model never got to write it - see _unfinished_reply.
CHANGING_TOOLS = {"rerun_with_changes", "rerun_evaluation"}


def build_agent(instruction: str = INSTRUCTION, description: str = "", tools=None):
    from google.adk.agents import Agent
    from google.genai import types

    return Agent(
        name=AGENT_NAME,
        model=settings.vertex_model,
        instruction=instruction,
        description=description or DEFAULT_DESCRIPTION,
        tools=list(tools if tools is not None else AGENT_TOOLS),
        # Answering a buyer from figures a rule engine produced is not a
        # creative task. At the model's default temperature the same question
        # comes back as a paragraph one time and a set of headings the next;
        # low and steady is what makes the replies read alike.
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
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

    An agent turn that simulated or created a run carries its exact tool
    arguments, so "yes, do it" runs what was simulated rather than what the
    prose happened to say about it. Those lines are marked as yours alone:
    they are a note to yourself, and repeating them at the buyer is exactly
    the kind of process talk the instruction rules out.
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
            turns.append(f"  [your own note, never shown to the buyer] "
                         f"{action.get('tool')} {arguments}{created}")
    return "Earlier in this conversation:\n" + "\n".join(turns) + "\n\n"


def _describe_action(action: dict) -> list[str]:
    tool = action.get("tool")
    if tool == "rerun_with_changes":
        lines = []
        for change in action.get("changes") or []:
            subject = change["key"] + (f" ({change['material']})" if change.get("material") else "")
            unit = f" {change['unit']}" if change.get("unit") else ""
            lines.append(f"- For this run only, {change['kind']} {subject}: "
                         f"{change.get('previous')} to {change['new']}{unit}")
        return lines + [f"- Created evaluation run {action['new_run_id']}",
                        "- The policy in force was not changed"]
    if tool == "rerun_evaluation":
        return [f"- Re-evaluated the basket under the policy in force into run "
                f"{action['new_run_id']}"]
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
    """Answer one request - explain, simulate, or evaluate into a new run.

    Returns the reply with what the turn did: new_run_id when the basket was
    re-evaluated into a new run, last_simulation (the arguments behind the
    what-if it ended on) and the actions themselves for the transcript. No
    path here changes the policy in force.

    Continuity is a transcript replayed into the prompt, not a persistent ADK
    session - _ask() still builds and tears down a fresh InMemoryRunner every
    call (see its docstring for why: a warm Cloud Run instance would
    otherwise accumulate open sessions across requests). The actual memory of
    the conversation lives in chat_message rows, passed in here as history,
    which is what makes it survive a request landing on a different Cloud Run
    instance than the one before it.
    """
    # The closing line repeats the one rule a model most often drops by the
    # time it has read a page of tool output, and it sits where the next
    # tokens are written from.
    prompt = (
        f"The evaluation run id is {run_id}. Use it for every run-scoped tool, "
        f"unless a change in this turn creates a newer run.\n\n"
        f"{_transcript(history)}New request from the buyer:\n\n{question}\n\n"
        f"Reply to the buyer: the answer first, in plain prose, with no mention "
        f"of tools or of the steps you took."
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
    """The reply when Vertex is unreachable or too slow.

    The same stored values the agent would have read, written the way the
    instruction asks the agent to write: the recommendation first, then each
    supplier in one line with the rule that decided it. A dump of every gate
    and promotion condition would be more complete and much worse to read -
    and this is a fallback the buyer sees without being told a model was
    involved at all.
    """
    from .tools import get_run_summary

    summary = get_run_summary(run_id)
    if not summary or summary.get("error"):
        return "That run could not be found."

    suppliers = summary["suppliers"]
    primary = next((s for s in suppliers if s.get("final_rank") == 1), None)

    if primary:
        lines = [f"{primary['supplier_name']} is the recommendation, at EUR "
                 f"{primary['total_landed_cost_eur']} total landed cost. "
                 f"{primary.get('primary_reason') or ''}".strip()]
    else:
        lines = ["No supplier is recommended in this run: none of them cleared "
                 "every gate."]

    others = [s for s in suppliers if s is not primary]
    if others:
        lines.append("")
        for supplier in sorted(others, key=lambda s: s.get("final_rank") or 99):
            standing = (f"rank {supplier['final_rank']}" if supplier.get("final_rank")
                        else "not ranked")
            lines.append(
                f"- {supplier['supplier_name']}: EUR "
                f"{supplier['total_landed_cost_eur']}, {standing}. "
                f"{supplier.get('primary_reason') or ''}".strip())

    return "\n".join(lines)
