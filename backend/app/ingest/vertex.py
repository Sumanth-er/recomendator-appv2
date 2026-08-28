"""Shared Vertex AI access.

One place to build the client and to ask for structured JSON, so the quotation
normalizer and the category strategy reader cannot drift apart on model,
temperature or response handling.
"""
from __future__ import annotations

import json
import logging
from ..config import settings
from .. import telemetry


log = logging.getLogger(__name__)


def client():
    from google import genai

    return genai.Client(
        vertexai=True,
        project=settings.project_id,
        location=settings.vertex_location,
    )


ATTEMPTS = 3


def _finish_reason(response) -> str | None:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return None
    return str(getattr(candidates[0], "finish_reason", None))


def generate_json(contents, schema: dict) -> dict:
    """Structured output against a fixed schema.

    Temperature stays at zero: these calls extract what a document says, so
    variation between runs is a defect rather than a feature.

    Retried up to ATTEMPTS times, because the failures seen here are transient
    rather than disagreements about the answer - a truncated response, an empty
    candidate, a 503. At temperature zero a retry of a genuinely different
    opinion would return the same text anyway, so this cannot paper over a bad
    extraction; it only stops one blip failing a whole document. MAX_TOKENS is
    not retried, since asking again gets the same oversized answer.
    """
    from google.genai import types

    config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        response_schema=schema,
    )

    with telemetry.tracer().start_as_current_span("vertex.generate_json") as span:
        telemetry.set_attributes(
            span,
            **{
                "gen_ai.system": "vertex_ai",
                "gen_ai.request.model": settings.vertex_model,
                "gen_ai.request.temperature": config.temperature,
            },
        )

        last_error: Exception | None = None
        for attempt in range(1, ATTEMPTS + 1):
            genai_client = client()
            try:
                response = genai_client.models.generate_content(
                    model=settings.vertex_model,
                    contents=contents,
                    config=config,
                )

                # google-genai exposes this as usage_metadata; reading `usage`
                # silently returned None and the token counts never appeared.
                usage = getattr(response, "usage_metadata", None)
                if usage is not None:
                    telemetry.set_attributes(
                        span,
                        **{
                            "gen_ai.usage.input_tokens": getattr(
                                usage, "prompt_token_count", None),
                            "gen_ai.usage.output_tokens": getattr(
                                usage, "candidates_token_count", None),
                            "gen_ai.usage.total_tokens": getattr(
                                usage, "total_token_count", None),
                        },
                    )

                text = getattr(response, "text", None)
                if not text:
                    reason = _finish_reason(response)
                    if reason and "MAX_TOKENS" in reason:
                        raise RuntimeError(
                            "Gemini hit the output token limit; the response was "
                            "cut off before any JSON was produced")
                    raise RuntimeError(
                        f"Gemini returned no text; finish_reason={reason}")

                return json.loads(text)

            except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                last_error = exc
                if "output token limit" in str(exc):
                    break
                if attempt < ATTEMPTS:
                    log.warning("Gemini call failed (attempt %d of %d), retrying: "
                                "%s: %s", attempt, ATTEMPTS, type(exc).__name__, exc)
            finally:
                try:
                    genai_client.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass

        telemetry.set_attributes(span, **{"gen_ai.attempts": ATTEMPTS})
        telemetry.record_exception(last_error or RuntimeError("Gemini call failed"))
        raise last_error or RuntimeError("Gemini call failed")



def pdf_part(content: bytes, mime_type: str = "application/pdf"):
    from google.genai import types

    return types.Part.from_bytes(data=content, mime_type=mime_type)
