#!/usr/bin/env python3

# Re-exports из tools/ (Phase B1)
from tools.constants import (
    WEBSOCKET_URI, RPC_BASE_URL, ORIGIN, USER_AGENT, TEXTINPUT_UA,
    DEFAULT_EXPERIMENTS, DEFAULT_SUPPORTED_FEATURES, _FRAME_SEQ,
    uuid7,
)
from tools.parser import _parse_nsb_card, compute_delta
from tools.transport import AliceRpcClient, AliceWsEngine

"""
Alice AI Unified Server.
"""

import argparse
import asyncio
import inspect
import json
import logging
import mimetypes
import os
import sys
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple
from datetime import datetime

import aiohttp
import websockets

from websockets.exceptions import ConnectionClosed
from agents import (
    AgentWidget, AliceAgent, AliceTurnResponse, SuggestionAction,
    ThinkingStep, ensure_agents_registered, registry,
)
from tools.effective import EffectiveMode, normalize_effective_mode
from agents.expert import ExpertAgent
from alice_auth import (
    AliceCredentials,
    load_or_request_credentials,
    resolve_auth_source,
    verify_credentials,
    load_credentials_with_cookies,
)

from tools.manager import (
    GenericAppAgent,
    AliceAgentManager,
    AliceMcpServer,
    ChatSession,
    SessionRegistry,
)







logger = logging.getLogger("alice_server")
















def create_curl_http_app(manager: Optional[AliceAgentManager]):
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse

    app = FastAPI(title="Alice HTTP API")

    @app.post("/chat")
    async def chat_post(request: Request):
        text = (await request.body()).decode("utf-8").strip()
        auth_header = request.headers.get("Authorization") or request.headers.get("X-Alice-Auth")

        mgr = manager
        transient_mgr = None
        try:
            if auth_header:
                custom_creds = resolve_auth_source(auth_header)
                if not custom_creds:
                    return PlainTextResponse("Auth Error: не удалось разобрать credentials\n", status_code=401)
                ok, msg = await verify_credentials(custom_creds)
                if not ok:
                    return PlainTextResponse("Auth Error: credentials отклонены\n", status_code=401)
                transient_mgr = AliceAgentManager(custom_creds, verbose=False)
                await transient_mgr.initialize()
                mgr = transient_mgr

            if not mgr:
                return PlainTextResponse("Auth Error\n", status_code=401)

            final_turn = None
            async for turn in mgr.send_message(text):
                final_turn = turn

            if not final_turn or final_turn.error:
                err = (final_turn.error if final_turn else "no response")
                return PlainTextResponse(f"Ошибка: {err}\n", status_code=500)
            return PlainTextResponse(final_turn.text + "\n")
        finally:
            if transient_mgr:
                try:
                    await transient_mgr.shutdown()
                except Exception:
                    pass

    return app


def _log_session_hint(creds: AliceCredentials):
    """Не логирует секреты. Использует только факты наличия и fingerprint UUID."""
    import hashlib
    cookie_dict = getattr(creds, "cookie_dict", None) or {}
    has_session = isinstance(cookie_dict, dict) and bool(cookie_dict.get("Session_id"))
    has_yuid = isinstance(cookie_dict, dict) and bool(cookie_dict.get("yandexuid"))
    device_uuid = getattr(creds, "device_uuid", "") or ""
    fingerprint = hashlib.sha256(device_uuid.encode("utf-8")).hexdigest()[:10] if device_uuid else "none"

    logger.info(
        "session_hint: Session_id=%s yandexuid=%s device_fp=%s auth_type=%s",
        "present" if has_session else "absent",
        "present" if has_yuid else "absent",
        fingerprint,
        getattr(creds, "auth_type", "unknown"),
    )
async def main_async(args):
    is_mcp = not args.http
    allow_interactive = not is_mcp and sys.stdin.isatty()

    creds = load_credentials_with_cookies(
        token_source=args.auth or args.token,
        cookies_source=args.cookies,
        allow_interactive=allow_interactive,
        strict_explicit=True,
    )

    manager = None
    if creds:
        logger.info(f"Healthcheck [{creds.auth_type.upper()}]")
        is_ok, msg = await verify_credentials(creds)
        if not is_ok:
            logger.error(f"[Healthcheck failed]: {msg}")
            if not is_mcp:
                sys.exit(1)
        else:
            logger.info(f"✓ {msg}")
            _log_session_hint(creds)
            manager = AliceAgentManager(
                creds, verbose=args.verbose,
                show_thinking=getattr(args, "thinking", False),
            )
            await manager.initialize()
    else:
        if is_mcp:
            logger.info("MCP в динамическом режиме.")
        else:
            logger.error("Учетные данные не найдены.")
            sys.exit(1)

    if args.http:
        import uvicorn
        from tools.gateway import ModelCatalog, attach_openai_routes

        async def _shared_factory():
            return manager

        app = create_curl_http_app(manager)
        catalog = ModelCatalog(manager_provider=lambda: manager)
        session_registry = SessionRegistry(manager_factory=_shared_factory)
        if manager is not None:
            try:
                manager.ws.enable_api_mode()
            except Exception:
                pass
            try:
                catalog.warm_up()
            except Exception as e:
                logger.warning(f"catalog.warm_up: {e!r}")
        attach_openai_routes(app, catalog, session_registry)
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            await session_registry.close()
            if manager:
                await manager.shutdown()
    else:
        mcp = AliceMcpServer(manager)
        try:
            await mcp.run_stdio()
        finally:
            if manager:
                await manager.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Yandex Alice AI Unified Server")
    parser.add_argument("--auth")
    parser.add_argument("--token")
    parser.add_argument("--cookies")
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--thinking", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()