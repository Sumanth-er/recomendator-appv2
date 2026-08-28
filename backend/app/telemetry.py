"""OpenTelemetry wiring: Cloud Trace for spans, Cloud Logging for log records.

Two rules hold throughout this module.

**Telemetry is best effort.** A missing exporter package, an unreachable
metadata server, a bad credential or a revoked IAM role downgrades the process
to "no telemetry". None of them stop the container starting or a request
finishing. Every entry point here catches broadly and keeps going, which is the
one place in this codebase where a bare ``except Exception`` is the correct
thing to write.

**The tracer provider is global.** That is what makes the ADK agent's own spans
work. ADK creates its spans through ``trace.get_tracer("gcp.vertex.agent")`` at
import time, which hands back a proxy that resolves against whatever provider is
installed when the span is actually started. So ``invocation``, ``agent_run``,
``call_llm`` and ``execute_tool`` land in the same trace as the HTTP request
that triggered them without ADK ever knowing this module exists - provided the
provider is registered, which is what setup() does.
"""
from __future__ import annotations

import logging
import os

from opentelemetry import trace

log = logging.getLogger(__name__)

_provider = None
_logger_provider = None
_log_handler = None
_state = "not started"

# Instrumentors are idempotent per process, but calling them twice logs a
# warning, so each one is recorded once it has run.
_instrumented: set[str] = set()


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def disabled() -> bool:
    """The master switch.

    OTEL_DISABLED=1 turns observability off completely: no exporter, no
    provider, and no instrumentation installed. That last part is the point -
    leaving the instrumentors in place would still wrap every request, every
    SQL statement and every outbound call to build spans that go nowhere.
    """
    return _flag("OTEL_DISABLED")


def enabled() -> bool:
    """Whether spans are being exported anywhere."""
    return _provider is not None


def _should_instrument(what: str) -> bool:
    """Instrumentation is only worth installing if something will export it."""
    if disabled():
        return False
    if _provider is None:
        # setup() either never ran or could not build an exporter. Either way
        # these spans have nowhere to go.
        log.debug("skipping %s instrumentation: no tracer provider", what)
        return False
    return True


def status() -> str:
    """One line for the startup log and /healthz."""
    if _instrumented:
        return f"{_state}; instrumented: {', '.join(sorted(_instrumented))}"
    return _state


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------

def _resource():
    """Service identity, enriched with Cloud Run details where we can get them.

    The GCP detector reads the metadata server. Off Google that call does not
    resolve, so it is attempted only when the environment looks like Cloud Run
    or GCE and its failure is not fatal either way.
    """
    from opentelemetry.sdk.resources import Resource

    base = Resource.create({
        "service.name": os.getenv(
            "OTEL_SERVICE_NAME", os.getenv("K_SERVICE", "sourcing-agent-backend")),
        "service.version": os.getenv("K_REVISION", "") or _engine_version(),
        "deployment.environment": os.getenv("DEPLOY_ENV", "poc"),
    })

    on_gcp = bool(os.getenv("K_SERVICE") or os.getenv("GCE_METADATA_HOST")
                  or os.getenv("GOOGLE_CLOUD_RUN_JOB"))
    if not on_gcp:
        return base

    try:
        from opentelemetry.resourcedetector.gcp_resource_detector import (
            GoogleCloudResourceDetector,
        )
        # raise_on_error stays False: an unavailable metadata server should cost
        # us the extra resource labels, not the whole trace pipeline.
        return base.merge(GoogleCloudResourceDetector(raise_on_error=False).detect())
    except Exception as exc:  # noqa: BLE001 - resource labels are optional
        log.debug("GCP resource detection unavailable: %s", exc)
        return base


def _engine_version() -> str:
    try:
        from .config import settings

        return settings.engine_version
    except Exception:  # noqa: BLE001
        return "unknown"


def _project_id() -> str:
    try:
        from .config import settings

        return settings.project_id or ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def setup() -> None:
    """Install the global tracer provider. Safe to call more than once."""
    global _provider, _logger_provider, _log_handler, _state

    if _provider is not None:
        return

    if disabled():
        _state = "disabled by OTEL_DISABLED"
        log.info("telemetry %s", _state)
        return

    project = _project_id()
    if not project:
        _state = "disabled: GCP_PROJECT is not set"
        log.info("telemetry %s", _state)
        return

    try:
        _setup_traces(project)
    except Exception as exc:  # noqa: BLE001 - never fatal
        _provider = None
        _state = f"trace export unavailable: {type(exc).__name__}: {exc}"
        log.warning("telemetry disabled - %s", _state)
        return

    # Logs are a separate pipeline and a separate failure. Losing the log
    # exporter must not cost us the traces we just wired up.
    try:
        _setup_logs(project)
    except Exception as exc:  # noqa: BLE001
        log.warning("cloud logging export unavailable, stdout logging still "
                    "applies: %s", exc)

    _quieten()
    log.info("telemetry ready - %s", _state)


def _setup_traces(project: str) -> None:
    global _provider, _state

    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    ratio = 1.0
    try:
        ratio = float(os.getenv("TRACE_SAMPLING_RATIO", "1.0"))
    except ValueError:
        log.warning("TRACE_SAMPLING_RATIO is not a number, sampling everything")

    provider = TracerProvider(
        resource=_resource(),
        sampler=ParentBased(TraceIdRatioBased(ratio)),
    )
    # ADK attaches the full LLM request and response to its spans, so batches
    # are larger here than the SDK defaults assume.
    provider.add_span_processor(
        BatchSpanProcessor(
            CloudTraceSpanExporter(project_id=project),
            max_queue_size=4096,
            max_export_batch_size=256,
            schedule_delay_millis=5000,
        )
    )
    trace.set_tracer_provider(provider)

    _provider = provider
    _state = f"traces -> Cloud Trace (project {project}, sampling {ratio})"


def _setup_logs(project: str) -> None:
    """Export log records through the OTel logging pipeline.

    Off by default. On Cloud Run the structured JSON this app already writes to
    stdout is picked up by Cloud Logging automatically and carries the same
    trace correlation, so enabling both puts every line in the log twice. Set
    OTEL_LOGS_EXPORT=1 when stdout is not being collected.
    """
    global _logger_provider, _log_handler, _state

    if not _flag("OTEL_LOGS_EXPORT"):
        _state += "; logs via stdout"
        return

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.cloud_logging import CloudLoggingExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    provider = LoggerProvider(resource=_resource())
    provider.add_log_record_processor(
        BatchLogRecordProcessor(CloudLoggingExporter(project_id=project)))
    set_logger_provider(provider)

    handler = LoggingHandler(logger_provider=provider)
    logging.getLogger().addHandler(handler)

    _logger_provider = provider
    _log_handler = handler
    _state += "; logs -> Cloud Logging API"


def _quieten() -> None:
    """Keep the exporters' own chatter out of the exported logs.

    Without this the Cloud Logging handler records the Cloud Logging client's
    debug output, which produces more records to export, which produces more
    debug output.
    """
    for name in ("opentelemetry", "google.cloud", "google.auth", "google.api_core",
                 "google_genai", "urllib3", "grpc"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Instrumentation - each one optional, each one isolated
# ---------------------------------------------------------------------------

def instrument_fastapi(app, excluded_urls: str = "/healthz") -> None:
    if "fastapi" in _instrumented or not _should_instrument("fastapi"):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # The ASGI layer emits a "http send" and "http receive" child span for
        # every request on top of the server span. They carry nothing this app
        # needs and roughly triple the HTTP span count, which is real money at
        # Cloud Trace's per-span pricing.
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls=excluded_urls, exclude_spans=["send", "receive"])
        _instrumented.add("fastapi")
    except Exception as exc:  # noqa: BLE001
        log.warning("FastAPI instrumentation unavailable: %s", exc)


def instrument_sqlalchemy(engine) -> None:
    if "sqlalchemy" in _instrumented or not _should_instrument("sqlalchemy"):
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine)
        _instrumented.add("sqlalchemy")
    except Exception as exc:  # noqa: BLE001
        log.warning("SQLAlchemy instrumentation unavailable: %s", exc)


def instrument_requests() -> None:
    if "requests" in _instrumented or not _should_instrument("requests"):
        return
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
        _instrumented.add("requests")
    except Exception as exc:  # noqa: BLE001
        log.warning("requests instrumentation unavailable: %s", exc)


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------

def tracer(name: str = "sourcing-agent"):
    """A tracer that is always safe to call.

    Before setup() this is a proxy that resolves later, and if setup() never
    succeeds it is a no-op. Callers never have to check.
    """
    return trace.get_tracer(name)


def set_attributes(span, **attributes) -> None:
    """Attach attributes without letting a bad value raise into business code."""
    try:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not set span attributes: %s", exc)


def record_exception(exc: BaseException) -> None:
    """Mark the current span failed. Silent when there is no live span."""
    try:
        from opentelemetry.trace import Status, StatusCode

        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
    except Exception:  # noqa: BLE001
        pass


def current_span_context():
    """Span context for handing a trace to a background task, or None."""
    try:
        ctx = trace.get_current_span().get_span_context()
        return ctx if ctx.is_valid else None
    except Exception:  # noqa: BLE001
        return None


def flush() -> None:
    """Push queued spans and logs out before the CPU is taken away.

    Cloud Run throttles a container's CPU once a response has been sent, which
    is exactly when a background task finishes and exactly when a batch
    processor would otherwise be waiting on its timer.
    """
    for provider in (_provider, _logger_provider):
        if provider is None:
            continue
        try:
            provider.force_flush(timeout_millis=5000)
        except Exception as exc:  # noqa: BLE001
            log.debug("telemetry flush failed: %s", exc)


def shutdown() -> None:
    for provider in (_provider, _logger_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.debug("telemetry shutdown failed: %s", exc)
