"""Shared Vertex AI access.

One place to build the client and to ask for structured JSON, so the quotation
normalizer and the category strategy reader cannot drift apart on model,
temperature or response handling.
"""
from __future__ import annotations

import json
import logging

from ..config import settings

log = logging.getLogger(__name__)


def client():
    from google import genai

    return genai.Client(
        vertexai=True,
        project=settings.project_id,
        location=settings.vertex_location,
    )


def generate_json(contents, schema: dict) -> dict:
    """Structured output against a fixed schema.

    Temperature is zero throughout: these calls extract what a document says,
    so variation between runs is a defect rather than a feature.
    """
    from google.genai import types

    response = client().models.generate_content(
        model=settings.vertex_model,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    return json.loads(response.text)


def pdf_part(content: bytes, mime_type: str = "application/pdf"):
    from google.genai import types

    return types.Part.from_bytes(data=content, mime_type=mime_type)
