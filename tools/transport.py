"""WS + RPC транспорт для Alice."""

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

from tools.constants import (
    WEBSOCKET_URI, RPC_BASE_URL, ORIGIN, USER_AGENT, TEXTINPUT_UA,
    DEFAULT_EXPERIMENTS, DEFAULT_SUPPORTED_FEATURES, _FRAME_SEQ,
    uuid7,
)
from tools.parser import _parse_nsb_card, compute_delta

logger = logging.getLogger("alice_transport")


class AliceRpcClient:
    def __init__(self, credentials: AliceCredentials, verbose: bool = False):
        self.creds = credentials
        self.verbose = verbose
        self.headers = self.creds.build_rpc_headers()

    async def get_alice_apps(self) -> Tuple[int, List[Dict[str, Any]], str]:
        url = f"{RPC_BASE_URL}/gproxy/get_alice_apps"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={}) as resp:
                    status = resp.status
                    body = await resp.text()
                    if status == 200:
                        return status, json.loads(body).get("apps", []), "OK"
                    return status, [], body[:250]
            except Exception as e:
                return 0, [], str(e)

    async def list_dialogs(self, limit: int = 15, not_entrypoint: bool = False) -> Tuple[int, List[Dict[str, Any]], str]:
        url = f"{RPC_BASE_URL}/dialog/list"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={"limit": limit, "hints": {"not_entrypoint": not_entrypoint}}) as resp:
                    status = resp.status
                    body = await resp.text()
                    if status == 200:
                        return status, json.loads(body).get("objects", []), "OK"
                    return status, [], body[:250]
            except Exception as e:
                return 0, [], str(e)

    async def delete_dialog(self, dialog_id: str) -> Tuple[int, str]:
        url = f"{RPC_BASE_URL}/dialog/remove_dialog"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={"dialog_id": dialog_id}) as resp:
                    return resp.status, await resp.text()
            except Exception as e:
                return 0, str(e)


class AliceWsEngine:
    def __init__(self, credentials: AliceCredentials, verbose: bool = False, show_thinking: bool = False):
        self.creds = credentials
        self.verbose = verbose
        self.show_thinking = show_thinking
        self.ws: Optional[Any] = None
        self.seq_number = 0
        self.session_id: Optional[str] = None
        self.is_connected = False
        self._pending_responses: Dict[str, asyncio.Queue] = {}
        self._running_task: Optional[asyncio.Task] = None
        self._active_agents_by_req: Dict[str, AliceAgent] = {}
        self._sync_event = asyncio.Event()
        self._listener_task: Optional[asyncio.Task] = None
        self._supervisor_task: Optional[asyncio.Task] = None
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_backoff = 2.0
        self._should_run = False
        self._api_mode = False
        self._consecutive_errors = 0

        self._upload_pending: Dict[str, asyncio.Queue] = {}
        self._upload_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._next_stream_id = 1

        self._full_content_dialogs: Set[str] = set()
        self._ping_dialogs: Set[str] = set()
        self._meeseeks_dialogs: Set[str] = set()
        self.effective_by_req: Dict[str, EffectiveMode] = {}
        self.requested_mode_by_req: Dict[str, str] = {}

        self.expert_trial_left: Optional[int] = None
        self.expert_trial_limit: Optional[int] = None

    def enable_api_mode(self):
        """В API-режиме все активные диалоги держат full-content."""
        self._api_mode = True

    def _icookie(self) -> str:
        cookies = getattr(self.creds, "cookie_dict", None) or {}
        ic = ""
        if isinstance(cookies, dict):
            ic = cookies.get("i", "") or ""
        return ic or (getattr(self.creds, "icookie", "") or "")

    def _resolve_experiments(self, agent: AliceAgent) -> List[str]:
        fn = getattr(agent, "build_experiments", None)
        if callable(fn):
            try:
                result = list(fn())
                if result:
                    return result
            except Exception as e:
                logger.warning(f"{agent.id}.build_experiments(): {e}")
        return list(DEFAULT_EXPERIMENTS)

    def _resolve_supported_features(self, agent: AliceAgent) -> List[str]:
        fn = getattr(agent, "build_supported_features", None)
        if callable(fn):
            try:
                result = list(fn())
                if result:
                    return result
            except Exception as e:
                logger.warning(f"{agent.id}.build_supported_features(): {e}")
        return list(DEFAULT_SUPPORTED_FEATURES)

    def _resolve_capabilities(self, agent: AliceAgent, dialog_id: str) -> List[Dict[str, Any]]:
        preset = getattr(agent, "preset", "") or ""
        mode = getattr(agent, "mode", "") or "Pro"
        if agent.id == "expert":
            preset = "agents_RnServiceAliceExpert"
            mode = "External"

        try:
            base = list(agent.build_capabilities(dialog_id, self.creds.device_uuid) or [])
        except Exception as e:
            logger.warning(f"{agent.id}.build_capabilities(): {e}")
            base = []

        if agent.id == "expert" and base:
            return base

        required_state = {
            "navigation_state": {"topmost_navigation_entry": {
                "id": "chat", "type": "Chat",
                "title": "Чат с Алисой", "can_navigate_back": False,
            }},
            "active_chat_dialog_context": {
                "dialog_id": dialog_id,
                "dialog_type": "DEDICATED_CHAT",
                "alice2_mode_info": {"preset": preset, "mode": mode},
            },
        }
        for cap in base:
            if "TAliceChatCapability" in cap.get("@type", ""):
                cap.setdefault("state", {})
                for key, val in required_state.items():
                    cap["state"].setdefault(key, val)

        if not base:
            base = [
                {"@type": "type.googleapis.com/NAlice.TAliceCapability",
                 "meta": {}, "parameters": {}, "state": {}},
                {
                    "@type": "type.googleapis.com/NAlice.TAliceChatCapability",
                    "meta": {"supported_directives": [
                        "SetChatRequestModeDirectiveType",
                        "ShowNotificationDirectiveType",
                        "CreateNewChatDirectiveType",
                    ]},
                    "parameters": {
                        "supports_rich_answers": True,
                        "supports_rich_suggests": True,
                        "supports_rich_summary": True,
                    },
                    "state": required_state,
                },
            ]
        return base

    async def _open(self):
        headers = self.creds.build_ws_headers()
        sig = inspect.signature(websockets.connect)
        ws_kwargs: Dict[str, Any] = {"ping_interval": None}
        if "max_size" in sig.parameters:
            ws_kwargs["max_size"] = 20 * 1024 * 1024
        if "additional_headers" in sig.parameters:
            ws_kwargs["additional_headers"] = headers
        else:
            ws_kwargs["extra_headers"] = headers
        self.ws = await websockets.connect(WEBSOCKET_URI, **ws_kwargs)
        self.is_connected = True
        self.seq_number = 0
        self._sync_event.clear()

    async def start(self):
        """Идемпотентный старт: ровно один listener + один supervisor."""
        if (self._should_run
                and self._listener_task is not None
                and not self._listener_task.done()):
            return
        self._should_run = True
        await self._open()
        self._listener_task = asyncio.create_task(self._listen_loop(), name="alice-ws-listener")
        self._running_task = self._listener_task
        self._supervisor_task = asyncio.create_task(self._supervise(), name="alice-ws-supervisor")
        try:
            await self._synchronize_state()
            await asyncio.wait_for(self._sync_event.wait(), timeout=7.0)
            if self.verbose:
                logger.info(f"Сессия Uniproxy: {self.session_id}")
        except asyncio.TimeoutError:
            logger.warning("Таймаут SynchronizeStateResponse")
        except Exception:
            for t in (self._listener_task, self._supervisor_task):
                if t is not None and not t.done():
                    t.cancel()
            self.is_connected = False
            try:
                if self.ws is not None:
                    await self.ws.close()
            except Exception:
                pass
            self.ws = None
            raise

    async def connect(self):
        """Обратная совместимость: connect() == start()."""
        await self.start()

    async def _supervise(self):
        """Единственный, кто переподключается. Reader запускается только здесь."""
        while self._should_run:
            if self.is_connected and self.ws is not None:
                await asyncio.sleep(1.0)
                continue
            async with self._reconnect_lock:
                if not self._should_run:
                    return
                try:
                    await self._open()
                    self._listener_task = asyncio.create_task(self._listen_loop(), name="alice-ws-listener")
                    self._running_task = self._listener_task
                    await self._synchronize_state()
                    try:
                        await asyncio.wait_for(self._sync_event.wait(), timeout=7.0)
                        if self.verbose:
                            logger.info(f"Сессия Uniproxy: {self.session_id}")
                    except asyncio.TimeoutError:
                        logger.warning("Таймаут SynchronizeStateResponse (reconnect)")
                    await self._flush_subscriptions()
                    self._reconnect_backoff = 2.0
                    logger.info("WS reconnected")
                except Exception as e:
                    logger.warning(f"WS reconnect failed: {e!r}; retry in {self._reconnect_backoff}s")
                    await asyncio.sleep(self._reconnect_backoff)
                    self._reconnect_backoff = min(self._reconnect_backoff * 2, 30.0)

    async def disconnect(self):
        self._should_run = False
        self.is_connected = False
        tasks = [
            t for t in (self._listener_task, self._supervisor_task, self._running_task)
            if t is not None and not t.done()
        ]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        self._listener_task = None
        self._supervisor_task = None
        self._running_task = None

    async def _send_json(self, data: dict):
        async with self._send_lock:
            if not self.ws or not self.is_connected:
                raise ConnectionError("WebSocket не подключен")
            if "event" in data and "header" in data["event"]:
                h = data["event"]["header"]
                key = (h.get("namespace"), h.get("name"))
                if key in _FRAME_SEQ:
                    h["seqNumber"] = _FRAME_SEQ[key]
            await self.ws.send(json.dumps(data, ensure_ascii=False))

    async def _synchronize_state(self):
        msg_id = uuid7()
        payload = {
            "event": {
                "header": {
                    "namespace": "System", "name": "SynchronizeState",
                    "messageId": msg_id,
                },
                "payload": {
                    "auth_token": self.creds.get_ws_auth_token(),
                    "uuid": self.creds.device_uuid,
                    "vins": {
                        "application": {
                            "app_id": "ru.yandex.webstandalone.desktop",
                            "platform": "linux",
                            "device_id": self.creds.device_uuid,
                            "uuid": self.creds.device_uuid,
                        }
                    },
                    "supported_features": DEFAULT_SUPPORTED_FEATURES,
                    "request": {"experiments": DEFAULT_EXPERIMENTS},
                    "speechkitVersion": "4.16.7",
                    "icookie": self._icookie(),
                    "client_analytics_info": {"client_url": "https://alice.yandex.ru/chat/"},
                },
            }
        }
        await self._send_json(payload)

    async def _flush_subscriptions(self):
        if not self.is_connected:
            return
        subs: List[Dict[str, Any]] = []
        for did in self._full_content_dialogs:
            subs.append({"id": did, "state": {"full_content": {}}})
        for did in self._ping_dialogs:
            if did in self._full_content_dialogs:
                continue
            subs.append({"id": did, "state": {"ping": {}}})
        if not subs:
            return
        payload = {
            "event": {
                "header": {"namespace": "System", "name": "ClientSubscriptionState",
                           "messageId": uuid7()},
                "payload": {"subscriptions": subs},
            }
        }
        try:
            await self._send_json(payload)
        except Exception as e:
            logger.debug(f"_flush_subscriptions: {e}")

    async def update_subscriptions(self, active_dialog_id: str, other_dialog_ids: Set[str]):
        if self._api_mode:
            self._full_content_dialogs = (
                {active_dialog_id} | set(other_dialog_ids) | self._meeseeks_dialogs
            )
            self._ping_dialogs = set()
        else:
            self._full_content_dialogs = {active_dialog_id} | self._meeseeks_dialogs
            self._ping_dialogs = other_dialog_ids - self._full_content_dialogs
        await self._flush_subscriptions()

    async def add_full_content(self, dialog_id: str):
        self._meeseeks_dialogs.add(dialog_id)
        self._full_content_dialogs.add(dialog_id)
        self._ping_dialogs.discard(dialog_id)
        await self._flush_subscriptions()

    async def remove_full_content(self, dialog_id: str):
        self._meeseeks_dialogs.discard(dialog_id)
        self._full_content_dialogs.discard(dialog_id)
        await self._flush_subscriptions()

    async def upload_file(
        self, file_path: str, title: Optional[str] = None,
        mime_type: Optional[str] = None, origin: str = "Chat", timeout: float = 120.0,
    ) -> Optional[Dict[str, Any]]:
        path = os.path.expanduser(file_path)
        if not os.path.isfile(path):
            logger.warning(f"upload_file: нет файла: {path}")
            return None
        size = os.path.getsize(path)
        if size == 0:
            return None
        title = title or os.path.basename(path)
        if not mime_type:
            guessed, _ = mimetypes.guess_type(path)
            mime_type = guessed or "application/octet-stream"

        async with self._upload_lock:
            stream_id = self._next_stream_id
            self._next_stream_id += 1
            message_id = uuid7()
            queue: asyncio.Queue = asyncio.Queue()
            self._upload_pending[message_id] = queue
            try:
                await self._send_json({
                    "event": {
                        "header": {"namespace": "File", "name": "Upload",
                                   "streamId": stream_id, "messageId": message_id},
                        "payload": {"file_upload_start": {
                            "mime_type": mime_type, "size": size,
                            "title": title, "origin": origin,
                        }},
                    }
                })
                try:
                    ack = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    return None
                start = ack.get("file_upload_start") or {}
                chunk_size = int(start.get("max_chunk_size") or 10 * 1024 * 1024)
                chunk_size = max(512 * 1024, min(chunk_size, 16 * 1024 * 1024))
                async with self._send_lock:
                    if not self.ws:
                        raise ConnectionError("WS закрыт")
                    with open(path, "rb") as f:
                        while True:
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            if not self.ws or not self.is_connected:
                                raise ConnectionError("WS закрыт")
                            await self.ws.send(chunk)
                await self._send_json({
                    "event": {
                        "header": {"namespace": "File", "name": "Upload",
                                   "streamId": stream_id, "messageId": uuid7(),
                                   "refMessageId": message_id},
                        "payload": {"chunk_upload_finish": {"is_last": True}},
                    }
                })
                deadline = asyncio.get_running_loop().time() + timeout
                finish: Dict[str, Any] = {}
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return None
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        return None
                    fp = msg.get("file_upload_finish")
                    if fp:
                        finish = fp
                        break
                file_guid = finish.get("file_guid")
                if not file_guid:
                    return None
                try:
                    if self.ws and self.is_connected:
                        await self.ws.send(json.dumps({
                            "streamcontrol": {"action": 0, "reason": 0,
                                              "streamId": stream_id, "messageId": message_id}
                        }, ensure_ascii=False))
                except Exception:
                    pass
                return {
                    "file_guid": file_guid,
                    "title": finish.get("title") or title,
                    "mime_type": finish.get("mime_type") or mime_type,
                    "size_bytes": finish.get("size_bytes") or size,
                    "preview_url": finish.get("preview_url"),
                    "extension": finish.get("extension"),
                }
            finally:
                self._upload_pending.pop(message_id, None)

    async def send_text_input(
        self, text: str, dialog_id: str, agent: AliceAgent,
        prev_req_id: Optional[str] = None, is_suggest: bool = False,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[AliceTurnResponse, None]:
        req_id = uuid7()

        capabilities = self._resolve_capabilities(agent, dialog_id)
        experiments = self._resolve_experiments(agent)
        supported_features = self._resolve_supported_features(agent)

        preset = "agents_RnServiceAliceExpert" if agent.id == "expert" else agent.preset
        mode = "External" if agent.id == "expert" else agent.mode

        header_dict: Dict[str, Any] = {
            "request_id": req_id, "dialog_id": dialog_id, "dialog_type": 2,
        }
        if prev_req_id:
            header_dict["prev_req_id"] = prev_req_id

        event_type = "suggested_input" if is_suggest else "text_input"

        payload = {
            "event": {
                "header": {
                    "namespace": "Vins", "name": "TextInput",
                    "messageId": req_id,
                },
                "payload": {
                    "application": {
                        "app_id": "ru.yandex.webstandalone.desktop",
                        "app_version": "unknown", "platform": "linux",
                        "os_version": TEXTINPUT_UA.lower(),
                        "uuid": self.creds.device_uuid,
                        "device_id": self.creds.device_uuid,
                        "lang": "ru-RU",
                        "client_time": datetime.now().strftime("%Y%m%dT%H%M%S"),
                        "timezone": "Europe/Moscow",
                        "timestamp": str(int(time.time())),
                    },
                    "header": header_dict,
                    "request": {
                        "event": {"type": event_type, "text": text},
                        "voice_session": False,
                        "experiments": experiments,
                        "uniproxy_options": {
                            "background_response_streaming_options": {
                                "request_with_shown_new_dialog": {}
                            }
                        },
                        "additional_options": {
                            "bass_options": {"user_agent": TEXTINPUT_UA, "screen_scale_factor": 1},
                            "origin_domain": "yandex.ru",
                            "supported_features": supported_features,
                            "unsupported_features": [],
                            "icookie": self._icookie(),
                        },
                        "environment_state": {"endpoints": [
                            {"id": self.creds.device_uuid, "capabilities": capabilities}
                        ]},
                    },
                    "format": "audio/ogg;codecs=opus",
                    "mime": "audio/ogg;codecs=opus",
                    "topic": "desktopgeneral",
                    "punctuation": False,
                    "alice_2_settings": {"preset": preset, "mode": mode},
                },
            }
        }

        if attachments:
            payload["event"]["payload"]["request"]["attachments"] = attachments

        queue: asyncio.Queue = asyncio.Queue()
        self._pending_responses[req_id] = queue
        self._active_agents_by_req[req_id] = agent
        self.requested_mode_by_req[req_id] = mode

        logger.info(
            f"TEXTINPUT preset={preset!r} mode={mode!r} "
            f"agent={agent.__class__.__name__} "
            f"caps={len(capabilities)} exp={len(experiments)} feat={len(supported_features)}"
        )

        try:
            await self._send_json(payload)
            while True:
                resp = await asyncio.wait_for(queue.get(), timeout=agent.timeout)
                if resp.is_finished:
                    resp.effective = self.effective_by_req.get(req_id)
                yield resp
                if resp.is_finished:
                    break
        except asyncio.TimeoutError:
            yield AliceTurnResponse(
                dialog_id=dialog_id, request_id=req_id,
                is_finished=True, error=f"Таймаут ответа ({agent.timeout} с).",
            )
        finally:
            self._pending_responses.pop(req_id, None)
            self._active_agents_by_req.pop(req_id, None)
            self.requested_mode_by_req.pop(req_id, None)
            self.effective_by_req.pop(req_id, None)

    def _handle_vins_response(self, header: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ref_id = header.get("refMessageId") or header.get("ref_message_id")
        if not ref_id:
            logger.debug("VinsResponse без refMessageId — пропускаю")
            return
        # Если запрос уже завершён (finally в send_text_input уже сработал) —
        # не создаём effective_by_req для мёртвого req_id, иначе утечка state.
        # Live-прогон 2026-10-28 показал: VinsResponse приходит через 4 секунды
        # после DeferredAliceResponse(is_last=true).
        if ref_id not in self._pending_responses:
            logger.debug("VinsResponse[ref=%s] late/unknown — skip", ref_id[-8:])
            return
        requested = self.requested_mode_by_req.get(ref_id)
        eff = normalize_effective_mode(payload, requested_mode=requested)
        self.effective_by_req[ref_id] = eff
        if eff.trial_left is not None:
            self.expert_trial_left = eff.trial_left
            self.expert_trial_limit = eff.trial_limit
        # uuid7: старшие 32 бита = ms timestamp, за 49 дней не меняются.
        # ref_id[:8] бесполезен для идентификации — берём последние 8 hex.
        logger.info(
            "VinsResponse[ref=%s] requested=%s effective=%s preset=%s src=%s degraded=%s",
            ref_id[-8:], eff.requested_mode, eff.effective_mode,
            eff.effective_preset, eff.source_path, eff.degraded,
        )

    def _update_trial_state(self, payload: Dict[str, Any]):
        eff = payload.get("alice_2_effective_settings") or {}
        trial = eff.get("trial_state") or {}
        left = trial.get("left_count")
        limit = trial.get("limit")
        if left is not None:
            self.expert_trial_left = int(left)
            self.expert_trial_limit = int(limit) if limit is not None else self.expert_trial_limit

    async def _listen_loop(self):
        accumulated_turns: Dict[str, AliceTurnResponse] = {}
        accumulated_text: Dict[str, str] = {}
        while self.is_connected:
            try:
                if not self.ws:
                    logger.warning("ws is None, break listener")
                    break
                raw_msg = await self.ws.recv()
                self._consecutive_errors = 0
                if not raw_msg:
                    continue

                data = json.loads(raw_msg)
                directive = data.get("directive", {})
                header = directive.get("header", {})
                name = header.get("name")
                ref_id = header.get("refMessageId") or header.get("ref_message_id")

                if name == "Ping":
                    pong = {"event": {"header": {
                        "namespace": "System", "name": "Pong",
                        "messageId": uuid7(),
                        "refMessageId": header.get("messageId"),
                    }, "payload": {}}}
                    try:
                        if self.ws and self.is_connected:
                            async with self._send_lock:
                                await self.ws.send(json.dumps(pong, ensure_ascii=False))
                    except Exception:
                        pass
                    continue

                if name == "SynchronizeStateResponse":
                    self.session_id = directive.get("payload", {}).get("SessionId")
                    self._sync_event.set()
                    continue

                if name == "VinsResponse":
                    self._handle_vins_response(header, directive.get("payload", {}))
                    continue

                if name == "Upload" and ref_id in self._upload_pending:
                    await self._upload_pending[ref_id].put(directive.get("payload", {}))
                    continue

                if name in ("ErrorMessage", "Error") and ref_id in self._pending_responses:
                    err_text = directive.get("payload", {}).get("message", "Ошибка Uniproxy")
                    turn = AliceTurnResponse(dialog_id="", request_id=ref_id,
                                            is_finished=True, error=err_text)
                    await self._pending_responses[ref_id].put(turn)
                    continue

                if name == "DeferredAliceResponse" and ref_id in self._pending_responses:
                    payload = directive.get("payload", {})
                    json_resp = payload.get("json_response", {})
                    base_resp = json_resp.get("base_response", {})
                    is_last = json_resp.get("is_last", False)
                    cards = base_resp.get("cards", [])

                    if ref_id not in accumulated_turns:
                        accumulated_turns[ref_id] = AliceTurnResponse(dialog_id="", request_id=ref_id)

                    turn = accumulated_turns[ref_id]
                    turn.is_finished = is_last
                    text_blocks: List[str] = []
                    turn.widgets = []

                    agent = self._active_agents_by_req.get(ref_id) or registry.get("pro")

                    for card in cards:
                        answer_text, thinking_steps, sources = _parse_nsb_card(card)

                        for step in thinking_steps:
                            if not any(
                                s.kind == step.kind
                                and s.title == step.title
                                and s.content == step.content
                                and s.tool == step.tool
                                for s in turn.thinking_steps
                            ):
                                turn.thinking_steps.append(step)

                        for src in sources:
                            if src not in turn.sources:
                                turn.sources.append(src)

                        if answer_text and answer_text not in text_blocks:
                            text_blocks.append(answer_text)
                        if answer_text or thinking_steps:
                            continue
                        result = agent.parse_card(card, turn, text_blocks)
                        if inspect.isawaitable(result):
                            result = await result
                        if result:
                            continue
                        # IMPORTANT: не strip() — иначе теряем пробелы в настоящих дельтах.
                        # Дедупликация по точному тексту блока, а не по strip-форме.
                        tc = card.get("text_card")
                        if tc and "text" in tc:
                            t = tc.get("text", "")
                            if t and t not in text_blocks:
                                text_blocks.append(t)
                        nsb = card.get("neuro_structured_blocks_card")
                        if nsb and not nsb.get("json_structured_data"):
                            pt = nsb.get("plain_text") or ""
                            if pt and pt not in text_blocks:
                                text_blocks.append(pt)

                    # Снимок/дельта нормализуются в накопительный текст.
                    prev_text = accumulated_text.get(ref_id, "")
                    new_text = "\n\n".join(text_blocks)
                    _delta, accum = compute_delta(prev_text, new_text)
                    accumulated_text[ref_id] = accum
                    # Финальную накопленную строку trim'аем один раз — сервер может
                    # прислать хвостовой/лидирующий пробел в последнем кадре.
                    turn.text = accum.strip() if is_last else accum

                    turn_copy = AliceTurnResponse(
                        dialog_id=turn.dialog_id,
                        request_id=turn.request_id,
                        thinking_logs=list(turn.thinking_logs),
                        thinking_steps=list(turn.thinking_steps),
                        text=turn.text,
                        widgets=list(turn.widgets),
                        suggestions=list(turn.suggestions),
                        sources=list(turn.sources),
                        is_finished=turn.is_finished,
                        error=turn.error,
                    )
                    await self._pending_responses[ref_id].put(turn_copy)
                    if is_last:
                        accumulated_turns.pop(ref_id, None)
                        accumulated_text.pop(ref_id, None)

            except ConnectionClosed:
                logger.warning("WS closed, listener exits — supervisor will reconnect")
                self.is_connected = False
                try:
                    if self.ws is not None:
                        await self.ws.close()
                except Exception:
                    pass
                self.ws = None
                return
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WS listener error: {e}")
                self._consecutive_errors = getattr(self, "_consecutive_errors", 0) + 1
                delay = min(0.02 * self._consecutive_errors, 5.0)
                await asyncio.sleep(delay)
