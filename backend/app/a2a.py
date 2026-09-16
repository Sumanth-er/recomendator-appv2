"""Optional Agent2Agent surface.

Off unless A2A_ENABLED=1, and nothing in here is imported while it is off, so
a service running without the flag behaves exactly as it did before this file
existed.

Why the routes are registered here rather than with ADK's to_a2a():
to_a2a() builds its own Starlette app and registers the A2A routes inside that
app's lifespan. Starlette does not run a mounted sub-app's lifespan, so
mounting it yields an app with no routes and no error to explain it. Adding the
routes to the app we already have keeps one process serving both surfaces and
puts the agent card on the well-known path that A2A clients actually probe.

The JSON-RPC endpoint is deliberately moved off "/" - the a2a default - so it
cannot collide with the SPA mount or with anything served at the root.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

log = logging.getLogger(__name__)

RPC_PATH = "/a2a"

# What the agent card tells other agents this service is for. The card is a
# public discovery document, so this is deliberately a summary rather than the
# agent's instruction.
DESCRIPTION = (
    "Answers questions about a completed semiconductor wet-chemicals quote "
    "comparison: supplier ranking, compliance gates, ceiling-price variance, "
    "the promotion rule, award allocation and renegotiation candidates. It can "
    "also simulate what-if changes without saving, and - when explicitly asked "
    "- evaluate the basket again into a new run under different thresholds, "
    "ceiling prices, volumes or compliance tiers. It never changes the policy "
    "in force: those values apply to the run it creates and to nothing else. "
    "Every figure comes from the rule engine. Callers must supply the "
    "evaluation run id."
)


def enabled() -> bool:
    return os.getenv("A2A_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def public_url() -> str:
    """Base URL other agents will call.

    The card advertises this, so it has to be the address reachable from
    outside - the Cloud Run service URL, not localhost. Getting it wrong does
    not break this service; it publishes a card pointing somewhere unreachable.
    """
    return os.getenv("A2A_PUBLIC_URL", "http://localhost:8080").rstrip("/")


async def install(app: FastAPI) -> bool:
    """Add the A2A routes to an existing app. Returns whether they were added.

    Never raises. A2A is an extra surface; failing to build it must not stop a
    service that was serving fine without it.
    """
    if not enabled():
        return False
    try:
        from a2a.server.apps import A2AFastAPIApplication
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.tasks import (
            InMemoryPushNotificationConfigStore, InMemoryTaskStore)
        from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
        from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
        from google.adk.artifacts import InMemoryArtifactService
        from google.adk.auth.credential_service.in_memory_credential_service import (
            InMemoryCredentialService)
        from google.adk.memory import InMemoryMemoryService
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from starlette.routing import Mount

        from .agent.agent import build_agent

        agent = build_agent(description=DESCRIPTION)

        def make_runner() -> Runner:
            # A2aAgentExecutor resolves this factory on the first task and
            # caches the runner it returns. In-memory services here are
            # per-instance state: see the note on A2A in infra/ENV.md.
            return Runner(
                app_name=agent.name,
                agent=agent,
                artifact_service=InMemoryArtifactService(),
                session_service=InMemorySessionService(),
                memory_service=InMemoryMemoryService(),
                credential_service=InMemoryCredentialService(),
            )

        # The card advertises the RPC endpoint; the route below has to be the
        # same path or callers read the card and then hit a 404.
        card = await AgentCardBuilder(
            agent=agent, rpc_url=f"{public_url()}{RPC_PATH}").build()

        handler = DefaultRequestHandler(
            agent_executor=A2aAgentExecutor(runner=make_runner),
            task_store=InMemoryTaskStore(),
            push_config_store=InMemoryPushNotificationConfigStore(),
        )
        A2AFastAPIApplication(
            agent_card=card, http_handler=handler
        ).add_routes_to_app(app, rpc_url=RPC_PATH)

        # A catch-all Mount("/") - the SPA, when SERVE_FRONTEND=1 - matches
        # every path, and Starlette matches in registration order, so anything
        # added after it is unreachable. Move those to the back.
        for route in [r for r in app.router.routes
                      if isinstance(r, Mount) and r.path in ("", "/")]:
            app.router.routes.remove(route)
            app.router.routes.append(route)

        log.info("a2a enabled: card at /.well-known/agent-card.json, rpc at %s%s",
                 public_url(), RPC_PATH)
        return True
    except Exception:
        log.exception("a2a setup failed; serving without it")
        return False