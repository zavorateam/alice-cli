#!/usr/bin/env python3
"""
Alice AI Unified Server (MCP stdio + curl HTTP API).
Поддерживает работу со всеми приложениями и агентами Алисы.
Включает компактный лог WS/RPC, устранение дублей виджетов и точное удаление через /dialog/remove_dialog.
"""

import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

import aiohttp
import websockets

from agents import AgentWidget, AliceAgent, AliceTurnResponse, SuggestionAction, registry

WEBSOCKET_URI = "wss://uniproxy.alice.yandex.ru/uni.ws"
RPC_BASE_URL = "https://rpc.alice.yandex.ru"
ORIGIN = "https://alice.yandex.ru"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0"
COOKIE_FILE_DEFAULT = ".alice_cookies"

DEFAULT_EXPERIMENTS = [
    "dont_skip_cancel_requests",
    "enable_parallel_requests_to_chats",
    "read_dialogs_for_unauthorized_users",
    "mm_allow_anonymous_request",
    "enable_external_skills_for_webdesktop_and_webtouch",
    "send_show_view_directive_on_supports_show_view_layer_content_interface",
    "standalone_alice_2_0",
    "mm_enable_protocol_scenario=WebAliceControls",
    "exp_flag_chat_dialog_history",
    "exp_flag_chat_dialog_history_main_context_save",
    "div2cards_in_external_skills_for_web_standalone",
    "enable_find_poi_standalone",
    "use_server_pings",
    "enable_onboarding_adaptive_size",
    "standalone_show_fullscreen_image_gallery_directive",
    "draw_picture_enable_controls",
    "alice_has_borders_div_paddings",
    "enable_new_colors_for_alice_chat",
    "erase_serialized_response_from_json_deferred_alice_response",
    "skills_standalone_use_div_render",
    "standalone_skill_card_cloud_ui",
]

DEFAULT_SUPPORTED_FEATURES = [
    "background_response_streaming_for_dialog_controls",
    "background_response_streaming_in_read_dialog",
    "background_response_streaming",
    "background_response_streaming_anon",
    "supports_bso_answer",
    "open_link",
    "server_action",
    "show_promo",
    "reminders_and_todos",
    "div2_cards",
    "player_pause_directive",
    "can_open_dialogs_in_tabs",
    "supports_streaming_response",
    "supports_rich_json_cards",
    "builtin_reaction",
    "open_link_by_button",
    "supports_origin_in_separate_card",
    "supports_new_sources_cards",
    "supports_markdown_response",
    "supported_save_chathistory",
    "supported_load_chathistory",
    "supports_unlimited_dialogs_creation",
    "supports_multi_model_dialogs",
    "print_text_in_message_view",
    "show_loader_directive",
    "supports_stringbody_in_div2_card",
    "supports_default_dialog_as_dedicated",
    "whisper",
]

logger = logging.getLogger("alice_server")


class GenericAppAgent(AliceAgent):
    def __init__(
        self,
        app_id: str,
        name: str,
        aliases: Optional[List[str]] = None,
        description: str = "",
    ):
        self.id = app_id
        self.name = name
        self.aliases = aliases or []
        self.type = "app"
        self.mode = "External"
        self.preset = "dialogovo_alice_apps_sticky"
        self.app_id = app_id
        self.description = description
        self.timeout = 60.0


def parse_cookies_file(content: str) -> Tuple[str, Dict[str, str]]:
    cookie_dict: Dict[str, str] = {}
    lines = content.strip().splitlines()
    is_netscape = any(l.startswith("# Netscape") or "\t" in l for l in lines)

    if is_netscape:
        for line in lines:
            line = line.strip()
            if not line or line.startswith("# HTTP") or line.startswith("# Netscape"):
                continue
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            parts = line.split("\t")
            if len(parts) >= 7:
                name = parts[5].strip()
                val = parts[6].strip()
                cookie_dict[name] = val
    else:
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for item in line.split(";"):
                item = item.strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    cookie_dict[k.strip()] = v.strip()

    if "alice_uuid" not in cookie_dict:
        if "yandexuid" in cookie_dict:
            cookie_dict["alice_uuid"] = cookie_dict["yandexuid"].zfill(32)
        else:
            cookie_dict["alice_uuid"] = "00000000000008672953191788425249"

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    return cookie_header, cookie_dict


def load_cookies(filepath: str) -> Tuple[str, Dict[str, str]]:
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
    else:
        env_cookie = os.environ.get("ALICE_COOKIES")
        if env_cookie:
            content = env_cookie.strip()
        else:
            raise FileNotFoundError(f"Файл кук '{filepath}' не найден!")
    return parse_cookies_file(content)


class AliceRpcClient:
    def __init__(self, cookies: str, uuid_val: str, verbose: bool = False):
        self.cookies = cookies
        self.uuid = uuid_val
        self.verbose = verbose
        self.headers = {
            "User-Agent": USER_AGENT,
            "Origin": ORIGIN,
            "Referer": "https://alice.yandex.ru/",
            "Cookie": self.cookies,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-ya-app-id": "ru.yandex.webstandalone.desktop",
            "x-ya-app-type": "other",
            "X-Ya-App-Id": "ru.yandex.webstandalone.desktop",
            "X-Ya-App-Type": "other",
            "X-Ya-Uuid": self.uuid,
            "X-Ya-Device-Id": self.uuid,
            "X-Ya-Language": "ru",
            "X-Ya-Supported-Features": ",".join(DEFAULT_SUPPORTED_FEATURES),
            "X-Ya-Application": json.dumps(
                {
                    "app_id": "ru.yandex.webstandalone.desktop",
                    "app_type": "other",
                    "uuid": self.uuid,
                    "device_id": self.uuid,
                    "lang": "ru",
                    "timezone": "Europe/Moscow",
                }
            ),
            "X-Ya-Experiments": json.dumps(DEFAULT_EXPERIMENTS),
        }

    async def get_alice_apps(self) -> Tuple[int, List[Dict[str, Any]], str]:
        url = f"{RPC_BASE_URL}/gproxy/get_alice_apps"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={}) as resp:
                    status = resp.status
                    body = await resp.text()
                    if self.verbose:
                        print(f"\033[34m[RPC]\033[0m POST /gproxy/get_alice_apps -> HTTP {status}")
                    if status == 200:
                        data = json.loads(body)
                        return status, data.get("apps", []), "OK"
                    return status, [], body[:250]
            except Exception as e:
                return 0, [], str(e)

    async def list_dialogs(self, limit: int = 15) -> Tuple[int, List[Dict[str, Any]], str]:
        url = f"{RPC_BASE_URL}/dialog/list"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(
                    url, json={"limit": limit, "hints": {"not_entrypoint": False}}
                ) as resp:
                    status = resp.status
                    body = await resp.text()
                    if self.verbose:
                        print(f"\033[34m[RPC]\033[0m POST /dialog/list -> HTTP {status}")
                    if status == 200:
                        data = json.loads(body)
                        return status, data.get("objects", []), "OK"
                    return status, [], body[:250]
            except Exception as e:
                return 0, [], str(e)

    async def delete_dialog(self, dialog_id: str) -> Tuple[int, str]:
        """Точный метод удаления чата через /dialog/remove_dialog"""
        url = f"{RPC_BASE_URL}/dialog/remove_dialog"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={"dialog_id": dialog_id}) as resp:
                    status = resp.status
                    body = await resp.text()
                    if self.verbose:
                        print(f"\033[34m[RPC]\033[0m POST /dialog/remove_dialog ({dialog_id[:10]}...) -> HTTP {status}")
                    return status, body[:250]
            except Exception as e:
                return 0, str(e)


class AliceWsEngine:
    def __init__(self, cookies: str, uuid_val: str, icookie_val: str, verbose: bool = False):
        self.cookies = cookies
        self.uuid = uuid_val
        self.icookie = icookie_val
        self.verbose = verbose
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.seq_number = 0
        self.session_id: Optional[str] = None
        self.is_connected = False
        self._pending_responses: Dict[str, asyncio.Queue] = {}
        self._running_task: Optional[asyncio.Task] = None
        self._active_agents_by_req: Dict[str, AliceAgent] = {}
        self._sync_event = asyncio.Event()

    async def connect(self):
        headers = {
            "User-Agent": USER_AGENT,
            "Origin": ORIGIN,
            "Cookie": self.cookies,
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "x-ya-app-id": "ru.yandex.webstandalone.desktop",
            "x-ya-app-type": "other",
            "X-Ya-Uuid": self.uuid,
            "X-Ya-Device-Id": self.uuid,
        }
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
        self._running_task = asyncio.create_task(self._listen_loop())

        await self._synchronize_state()
        try:
            await asyncio.wait_for(self._sync_event.wait(), timeout=7.0)
            if self.verbose:
                print(f"\033[32m[WS SYNC]\033[0m Сессия Uniproxy готова: {self.session_id}")
        except asyncio.TimeoutError:
            logger.warning("Таймаут ожидания SynchronizeStateResponse, продолжаем...")

    async def disconnect(self):
        self.is_connected = False
        if self._running_task:
            self._running_task.cancel()
        if self.ws:
            await self.ws.close()

    async def _send_json(self, data: dict):
        if not self.ws or not self.is_connected:
            raise ConnectionError("WebSocket не подключен")
        self.seq_number += 1
        if "event" in data and "header" in data["event"]:
            data["event"]["header"]["seqNumber"] = self.seq_number
        await self.ws.send(json.dumps(data, ensure_ascii=False))

    async def _synchronize_state(self):
        msg_id = str(uuid.uuid4())
        payload = {
            "event": {
                "header": {
                    "namespace": "System",
                    "name": "SynchronizeState",
                    "seqNumber": 1,
                    "messageId": msg_id,
                },
                "payload": {
                    "auth_token": str(uuid.uuid4()),
                    "uuid": self.uuid,
                    "vins": {
                        "application": {
                            "app_id": "ru.yandex.webstandalone.desktop",
                            "platform": "linux",
                            "device_id": self.uuid,
                            "uuid": self.uuid,
                        }
                    },
                    "supported_features": DEFAULT_SUPPORTED_FEATURES,
                    "request": {"experiments": DEFAULT_EXPERIMENTS},
                    "speechkitVersion": "4.16.7",
                    "icookie": self.icookie,
                    "client_analytics_info": {"client_url": "https://alice.yandex.ru/chat/"},
                },
            }
        }
        await self._send_json(payload)

    async def update_subscriptions(self, active_dialog_id: str, other_dialog_ids: Set[str]):
        subs = [{"id": did, "state": {"ping": {}}} for did in other_dialog_ids if did != active_dialog_id]
        subs.append({"id": active_dialog_id, "state": {"full_content": {}}})
        payload = {
            "event": {
                "header": {
                    "namespace": "System",
                    "name": "ClientSubscriptionState",
                    "messageId": str(uuid.uuid4()),
                },
                "payload": {"subscriptions": subs},
            }
        }
        await self._send_json(payload)

    async def send_text_input(
        self,
        text: str,
        dialog_id: str,
        agent: AliceAgent,
        prev_req_id: Optional[str] = None,
        is_suggest: bool = False,
    ) -> AsyncGenerator[AliceTurnResponse, None]:
        req_id = str(uuid.uuid4())
        client_time = datetime.now().strftime("%Y%m%dT%H%M%S")
        timestamp = str(int(time.time()))

        capabilities = agent.build_capabilities(dialog_id, self.uuid)

        header_dict: Dict[str, Any] = {
            "request_id": req_id,
            "dialog_id": dialog_id,
            "dialog_type": 2,
        }
        if prev_req_id:
            header_dict["prev_req_id"] = prev_req_id

        event_type = "suggested_input" if is_suggest else "text_input"

        payload = {
            "event": {
                "header": {
                    "namespace": "Vins",
                    "name": "TextInput",
                    "messageId": req_id,
                    "seqNumber": self.seq_number + 1,
                },
                "payload": {
                    "application": {
                        "app_id": "ru.yandex.webstandalone.desktop",
                        "app_version": "unknown",
                        "platform": "linux",
                        "os_version": USER_AGENT.lower(),
                        "uuid": self.uuid,
                        "device_id": self.uuid,
                        "lang": "ru-RU",
                        "client_time": client_time,
                        "timezone": "Europe/Moscow",
                        "timestamp": timestamp,
                    },
                    "header": header_dict,
                    "request": {
                        "event": {"type": event_type, "text": text},
                        "voice_session": False,
                        "experiments": DEFAULT_EXPERIMENTS,
                        "uniproxy_options": {"background_response_streaming_options": {}},
                        "additional_options": {
                            "bass_options": {"user_agent": USER_AGENT, "screen_scale_factor": 1},
                            "origin_domain": "yandex.ru",
                            "supported_features": DEFAULT_SUPPORTED_FEATURES,
                            "unsupported_features": [],
                            "icookie": self.icookie,
                        },
                        "environment_state": {"endpoints": [{"id": self.uuid, "capabilities": capabilities}]},
                    },
                    "format": "audio/ogg;codecs=opus",
                    "mime": "audio/ogg;codecs=opus",
                    "topic": "desktopgeneral",
                    "punctuation": False,
                    "alice_2_settings": {"preset": agent.preset, "mode": agent.mode},
                },
            }
        }

        if self.verbose:
            print(f"\033[36m[WS SEND]\033[0m TextInput (seq={self.seq_number + 1}) | "
                  f"dialog: \033[1m{dialog_id[:10]}...\033[0m | "
                  f"agent: \033[33m{agent.id}\033[0m ({agent.name}) | "
                  f"text: \"{text[:45]}{'...' if len(text) > 45 else ''}\"")

        queue: asyncio.Queue = asyncio.Queue()
        self._pending_responses[req_id] = queue
        self._active_agents_by_req[req_id] = agent

        await self._send_json(payload)

        try:
            while True:
                resp = await asyncio.wait_for(queue.get(), timeout=agent.timeout)
                yield resp
                if resp.is_finished:
                    break
        except asyncio.TimeoutError:
            yield AliceTurnResponse(
                dialog_id=dialog_id,
                request_id=req_id,
                is_finished=True,
                error=f"Таймаут ответа Uniproxy ({agent.timeout} сек).",
            )
        finally:
            self._pending_responses.pop(req_id, None)
            self._active_agents_by_req.pop(req_id, None)

    async def _listen_loop(self):
        accumulated_turns: Dict[str, AliceTurnResponse] = {}

        while self.is_connected:
            try:
                raw_msg = await self.ws.recv()
                if not raw_msg:
                    continue

                data = json.loads(raw_msg)
                directive = data.get("directive", {})
                header = directive.get("header", {})
                name = header.get("name")
                ref_id = header.get("refMessageId") or header.get("ref_message_id")

                if name == "Ping":
                    pong = {
                        "event": {
                            "header": {
                                "namespace": "System",
                                "name": "Pong",
                                "messageId": str(uuid.uuid4()),
                                "refMessageId": header.get("messageId"),
                            },
                            "payload": {},
                        }
                    }
                    await self._send_json(pong)
                    continue

                if name == "SynchronizeStateResponse":
                    self.session_id = directive.get("payload", {}).get("SessionId")
                    self._sync_event.set()
                    continue

                if name == "VinsResponse":
                    d_payload = directive.get("payload", {})
                    qs = d_payload.get("response", {}).get("quality_storage", {})
                    win_reason = qs.get("post_win_reason", "unknown")
                    predicts = qs.get("post_predicts", {})
                    eff_settings = d_payload.get("effective_alice_2_settings", {})

                    winner = list(predicts.keys())[0] if predicts else "Standard"
                    if self.verbose:
                        print(f"\033[32m[WS RECV]\033[0m VinsResponse | winner: \033[1m{winner}\033[0m ({win_reason}) | "
                              f"effective: {eff_settings.get('preset', 'none')}")
                    continue

                if name in ("ErrorMessage", "Error") and ref_id in self._pending_responses:
                    err_text = directive.get("payload", {}).get("message", "Ошибка Uniproxy")
                    turn = AliceTurnResponse(dialog_id="", request_id=ref_id, is_finished=True, error=err_text)
                    await self._pending_responses[ref_id].put(turn)
                    continue

                if name == "DeferredAliceResponse" and ref_id in self._pending_responses:
                    payload = directive.get("payload", {})
                    json_resp = payload.get("json_response", {})
                    base_resp = json_resp.get("base_response", {})
                    is_last = json_resp.get("is_last", False)
                    partial_num = json_resp.get("response_partial_num", 0)
                    cards = base_resp.get("cards", [])

                    if ref_id not in accumulated_turns:
                        accumulated_turns[ref_id] = AliceTurnResponse(dialog_id="", request_id=ref_id)

                    turn = accumulated_turns[ref_id]
                    turn.is_finished = is_last

                    # Свежий список для каждого чанка полностью исключает дубликаты виджетов и текста
                    text_blocks: List[str] = []
                    turn.widgets = []

                    agent = self._active_agents_by_req.get(ref_id) or registry.get("pro")

                    for card in cards:
                        handled = agent.parse_card(card, turn, text_blocks)
                        if not handled:
                            tc = card.get("text_card")
                            if tc and "text" in tc:
                                t = tc["text"].strip()
                                if t and t not in text_blocks:
                                    text_blocks.append(t)

                            nsb = card.get("neuro_structured_blocks_card")
                            if nsb and "plain_text" in nsb:
                                pt = nsb["plain_text"].strip()
                                if pt and pt not in text_blocks:
                                    text_blocks.append(pt)

                    turn.text = "\n\n".join(text_blocks)

                    if self.verbose:
                        card_types = [
                            c.get("rich_uicard", {}).get("uicomponent_name") or list(c.keys())[0]
                            for c in cards
                        ]
                        if card_types:
                            print(f"\033[35m[WS RECV]\033[0m DeferredAliceResponse #{partial_num}"
                                  f"{' [LAST]' if is_last else ''} | cards: {card_types}")

                    turn_copy = AliceTurnResponse(
                        dialog_id=turn.dialog_id,
                        request_id=turn.request_id,
                        thinking_logs=list(turn.thinking_logs),
                        text=turn.text,
                        widgets=list(turn.widgets),
                        suggestions=list(turn.suggestions),
                        is_finished=turn.is_finished,
                        error=turn.error,
                    )
                    await self._pending_responses[ref_id].put(turn_copy)

                    if is_last:
                        accumulated_turns.pop(ref_id, None)

            except websockets.exceptions.ConnectionClosed:
                await asyncio.sleep(2)
                try:
                    await self.connect()
                except Exception:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Listener error: {e}")


class AliceAgentManager:
    def __init__(self, cookies: str, cookie_dict: Dict[str, str], verbose: bool = False):
        self.cookies = cookies
        self.cookie_dict = cookie_dict
        self.verbose = verbose

        self.uuid = cookie_dict.get("alice_uuid", "00000000000008672953191788425249")
        self.icookie = cookie_dict.get("i", "")

        self.rpc = AliceRpcClient(self.cookies, self.uuid, verbose=self.verbose)
        self.ws = AliceWsEngine(self.cookies, self.uuid, self.icookie, verbose=self.verbose)

        self.active_dialog_id: Optional[str] = None
        self.active_agent_id: str = "pro"
        self.dialog_prev_req_ids: Dict[str, Optional[str]] = {}
        self.known_dialogs: Set[str] = set()
        self.ephemeral_dialogs: Set[str] = set()

    async def initialize(self):
        status, apps, err = await self.rpc.get_alice_apps()
        if status == 200:
            for app in apps:
                app_id_val = app.get("app_id")
                app_name_val = app.get("name", "")
                if app_id_val:
                    existing = registry.get(app_id_val)
                    if not existing or existing.id == "pro":
                        dyn_app = GenericAppAgent(
                            app_id=app_id_val,
                            name=app_name_val or "Приложение",
                            aliases=[app_name_val.lower()] if app_name_val else [],
                            description=app.get("description", ""),
                        )
                        registry.register(dyn_app)

        await self.ws.connect()

        d_status, recent_dialogs, _ = await self.rpc.list_dialogs(limit=5)
        for d in recent_dialogs:
            did = d.get("dialog", {}).get("dialog_id")
            if did:
                self.known_dialogs.add(did)
                if not self.active_dialog_id:
                    self.active_dialog_id = did

        if not self.active_dialog_id:
            self.active_dialog_id = str(uuid.uuid4())
            self.known_dialogs.add(self.active_dialog_id)

        await self.ws.update_subscriptions(self.active_dialog_id, self.known_dialogs)

    async def create_new_dialog(self, agent_id: Optional[str] = None, is_ephemeral: bool = False) -> str:
        agent = registry.get(agent_id or self.active_agent_id)
        new_id = str(uuid.uuid4())
        self.known_dialogs.add(new_id)
        self.active_dialog_id = new_id
        self.active_agent_id = agent.id
        self.dialog_prev_req_ids[new_id] = None

        if is_ephemeral:
            self.ephemeral_dialogs.add(new_id)

        await self.ws.update_subscriptions(new_id, self.known_dialogs)
        return new_id

    async def cleanup_ephemeral_dialogs(self):
        """Удаляет с серверов Яндекса все временные диалоги текущей сессии."""
        if not self.ephemeral_dialogs:
            return
        for did in list(self.ephemeral_dialogs):
            status, _ = await self.rpc.delete_dialog(did)
            if status == 200 and self.verbose:
                print(f"\033[33m✓ Временный диалог {did[:10]}... удален с сервера.\033[0m")
            self.known_dialogs.discard(did)
            self.ephemeral_dialogs.discard(did)

    async def send_message(
        self,
        text: str,
        agent_id: Optional[str] = None,
        dialog_id: Optional[str] = None,
        is_suggest: bool = False,
    ) -> AsyncGenerator[AliceTurnResponse, None]:
        agent = registry.get(agent_id or self.active_agent_id)
        target_dialog_id = dialog_id or self.active_dialog_id or await self.create_new_dialog(agent.id)

        self.active_dialog_id = target_dialog_id
        self.active_agent_id = agent.id
        self.known_dialogs.add(target_dialog_id)

        prev_req_id = self.dialog_prev_req_ids.get(target_dialog_id)

        last_turn: Optional[AliceTurnResponse] = None
        async for turn in self.ws.send_text_input(
            text=text,
            dialog_id=target_dialog_id,
            agent=agent,
            prev_req_id=prev_req_id,
            is_suggest=is_suggest,
        ):
            turn.dialog_id = target_dialog_id
            last_turn = turn
            yield turn

        if last_turn and not last_turn.error:
            self.dialog_prev_req_ids[target_dialog_id] = last_turn.request_id

    async def shutdown(self):
        await self.cleanup_ephemeral_dialogs()
        await self.ws.disconnect()


class AliceMcpServer:
    def __init__(self, manager: AliceAgentManager):
        self.manager = manager

    def _get_tools_definition(self) -> List[Dict[str, Any]]:
        agents_list = [f"'{a.id}' ({a.name})" for a in registry.list_all()]
        agents_str = ", ".join(agents_list)
        return [
            {
                "name": "alice_list_agents",
                "description": "Возвращает каталог всех доступных агентов, моделей и приложений Алисы",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "alice_chat",
                "description": f"Отправить запрос Алисе. Доступные агенты: {agents_str}",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Текст сообщения"},
                        "agent": {
                            "type": "string",
                            "description": "Идентификатор агента: taxi, lavka, market, gas, legal, best_price, deep_research, pro, base",
                            "default": "pro",
                        },
                        "dialog_id": {"type": "string", "description": "ID диалога (опционально)"},
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "alice_click_action",
                "description": "Выбрать подсказку, адрес или нажать кнопку действия",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action_text": {"type": "string", "description": "Адрес, название тарифа или действие"},
                        "dialog_id": {"type": "string", "description": "ID диалога (опционально)"},
                    },
                    "required": ["action_text"],
                },
            },
            {
                "name": "alice_new_chat",
                "description": "Открыть новый диалог для приложения или агента",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "description": "Агент для нового чата", "default": "pro"},
                        "ephemeral": {"type": "boolean", "description": "Удалить диалог после закрытия", "default": False},
                    },
                    "required": [],
                },
            },
        ]

    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "alice_list_agents":
            agents_data = [
                {
                    "id": a.id,
                    "name": a.name,
                    "type": a.type,
                    "mode": a.mode,
                    "aliases": a.aliases,
                    "description": a.description,
                }
                for a in registry.list_all()
            ]
            return {"content": [{"type": "text", "text": json.dumps(agents_data, ensure_ascii=False, indent=2)}]}

        elif tool_name == "alice_chat":
            msg = arguments.get("message", "")
            agent = arguments.get("agent", "pro")
            did = arguments.get("dialog_id")

            final_turn = None
            async for turn in self.manager.send_message(msg, agent_id=agent, dialog_id=did):
                final_turn = turn

            if not final_turn:
                return {"isError": True, "content": [{"type": "text", "text": "Не получен ответ от Алисы"}]}
            if final_turn.error:
                return {"isError": True, "content": [{"type": "text", "text": f"Ошибка: {final_turn.error}"}]}

            result_obj = {
                "dialog_id": final_turn.dialog_id,
                "text": final_turn.text,
                "thinking_steps": final_turn.thinking_logs,
                "widgets": [w.to_dict() for w in final_turn.widgets],
                "suggested_actions": [s.to_dict() for s in final_turn.suggestions],
            }
            return {"content": [{"type": "text", "text": json.dumps(result_obj, ensure_ascii=False, indent=2)}]}

        elif tool_name == "alice_click_action":
            action_text = arguments.get("action_text", "")
            did = arguments.get("dialog_id") or self.manager.active_dialog_id

            final_turn = None
            async for turn in self.manager.send_message(action_text, dialog_id=did, is_suggest=True):
                final_turn = turn

            if not final_turn or final_turn.error:
                return {"isError": True, "content": [{"type": "text", "text": final_turn.error if final_turn else "Ошибка"}]}

            result_obj = {
                "dialog_id": final_turn.dialog_id,
                "text": final_turn.text,
                "widgets": [w.to_dict() for w in final_turn.widgets],
                "suggested_actions": [s.to_dict() for s in final_turn.suggestions],
            }
            return {"content": [{"type": "text", "text": json.dumps(result_obj, ensure_ascii=False, indent=2)}]}

        elif tool_name == "alice_new_chat":
            ag = arguments.get("agent", "pro")
            eph = arguments.get("ephemeral", False)
            new_id = await self.manager.create_new_dialog(agent_id=ag, is_ephemeral=eph)
            return {"content": [{"type": "text", "text": f"Создан диалог ID: {new_id} [агент: {ag}, ephemeral: {eph}]"}]}

        return {"isError": True, "content": [{"type": "text", "text": f"Неизвестный инструмент: {tool_name}"}]}

    async def run_stdio(self):
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_event_loop())

        while True:
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            try:
                req = json.loads(line_str)
                req_id = req.get("id")
                method = req.get("method")
                params = req.get("params", {})

                if method == "initialize":
                    res = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "yandex-alice-agents-server", "version": "3.3.0"},
                        },
                    }
                elif method == "notifications/initialized":
                    continue
                elif method == "ping":
                    res = {"jsonrpc": "2.0", "id": req_id, "result": {}}
                elif method == "tools/list":
                    res = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"tools": self._get_tools_definition()},
                    }
                elif method == "tools/call":
                    tool_name = params.get("name")
                    arguments = params.get("arguments", {})
                    call_result = await self.handle_tool_call(tool_name, arguments)
                    res = {"jsonrpc": "2.0", "id": req_id, "result": call_result}
                else:
                    res = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    }

                writer.write((json.dumps(res, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()

            except Exception as e:
                err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
                writer.write((json.dumps(err) + "\n").encode("utf-8"))
                await writer.drain()


def create_curl_http_app(manager: AliceAgentManager):
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse, StreamingResponse

    app = FastAPI(title="Alice curl Terminal API", docs_url=None, redoc_url=None)

    @app.get("/agents")
    def get_agents_curl():
        lines = [f"{'ID':<14} {'ТИП':<8} {'ИМЯ':<30} {'ОПИСАНИЕ':<35}", "-" * 85]
        for a in registry.list_all():
            lines.append(f"{a.id:<14} [{a.type.upper():<5}] {a.name:<30} {a.description}")
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.get("/dialogs")
    def get_dialogs_curl():
        lines = [f"Active Dialog ID: {manager.active_dialog_id} (Agent: {manager.active_agent_id})", "Known Dialogs:"]
        for did in manager.known_dialogs:
            marker = " <-- ACTIVE" if did == manager.active_dialog_id else ""
            eph_marker = " [EPHEMERAL]" if did in manager.ephemeral_dialogs else ""
            lines.append(f"  - {did}{eph_marker}{marker}")
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.post("/dialogs/new")
    async def create_new_dialog_curl(request: Request):
        agent = request.query_params.get("agent", "pro")
        eph = request.query_params.get("ephemeral", "0") in ("1", "true")
        new_id = await manager.create_new_dialog(agent_id=agent, is_ephemeral=eph)
        return PlainTextResponse(f"Created new active dialog: {new_id} [agent: {agent}, ephemeral: {eph}]\n")

    @app.delete("/dialogs/{dialog_id}")
    async def delete_dialog_curl(dialog_id: str):
        status, resp = await manager.rpc.delete_dialog(dialog_id)
        manager.known_dialogs.discard(dialog_id)
        manager.ephemeral_dialogs.discard(dialog_id)
        return PlainTextResponse(f"Deleted dialog {dialog_id}: HTTP {status}\n")

    @app.post("/chat")
    async def chat_curl_post(request: Request):
        agent = request.query_params.get("agent", manager.active_agent_id)
        dialog_id = request.query_params.get("dialog_id")
        stream_param = request.query_params.get("stream")
        stream_mode = True if stream_param is None else stream_param in ("1", "true")
        is_ephemeral = request.query_params.get("ephemeral", "0") in ("1", "true")

        raw_body = await request.body()
        text = raw_body.decode("utf-8").strip()

        is_suggest = False
        if text.startswith("{") and text.endswith("}"):
            try:
                jb = json.loads(text)
                text = jb.get("message") or jb.get("text", text)
                agent = jb.get("agent", agent)
                dialog_id = jb.get("dialog_id", dialog_id)
                is_suggest = jb.get("is_suggest", False)
                is_ephemeral = jb.get("ephemeral", is_ephemeral)
            except Exception:
                pass

        if not text:
            return PlainTextResponse("Ошибка: тело запроса пустое.\n", status_code=400)

        ephemeral_id = None
        if is_ephemeral and not dialog_id:
            ephemeral_id = await manager.create_new_dialog(agent_id=agent, is_ephemeral=True)
            dialog_id = ephemeral_id

        if stream_mode:
            async def text_streamer():
                printed_len = 0
                last_thought = ""
                try:
                    async for turn in manager.send_message(text, agent_id=agent, dialog_id=dialog_id, is_suggest=is_suggest):
                        if turn.error:
                            yield f"\n[Ошибка]: {turn.error}\n"
                            break

                        if turn.thinking_logs and turn.thinking_logs[-1] != last_thought and not turn.text:
                            last_thought = turn.thinking_logs[-1]
                            yield f"[Думает: {last_thought}]\n"

                        if turn.text:
                            delta = turn.text[printed_len:]
                            if delta:
                                printed_len = len(turn.text)
                                yield delta

                        if turn.is_finished:
                            yield "\n"
                            if turn.widgets:
                                seen_urls = set()
                                for w in turn.widgets:
                                    if w.url and w.url in seen_urls:
                                        continue
                                    if w.url:
                                        seen_urls.add(w.url)
                                    yield f"\n--- [📦 {w.title or 'Виджет'}] ---\n"
                                    if w.url:
                                        yield f"Ссылка: {w.url}\n"
                            if turn.suggestions:
                                yield "\nПодсказки / Тарифы / Адреса:\n"
                                for i, s in enumerate(turn.suggestions, start=1):
                                    yield f"  [{i}] {s.title}\n"
                finally:
                    if ephemeral_id:
                        await manager.rpc.delete_dialog(ephemeral_id)
                        manager.known_dialogs.discard(ephemeral_id)
                        manager.ephemeral_dialogs.discard(ephemeral_id)

            return StreamingResponse(text_streamer(), media_type="text/plain; charset=utf-8")

        final_turn = None
        try:
            async for turn in manager.send_message(text, agent_id=agent, dialog_id=dialog_id, is_suggest=is_suggest):
                final_turn = turn
        finally:
            if ephemeral_id:
                await manager.rpc.delete_dialog(ephemeral_id)
                manager.known_dialogs.discard(ephemeral_id)
                manager.ephemeral_dialogs.discard(ephemeral_id)

        if not final_turn:
            return PlainTextResponse("Ошибка: нет ответа от сервера Алисы.\n", status_code=504)

        return PlainTextResponse(final_turn.text + "\n")

    return app


async def main_async(args):
    cookie_str, cookie_dict = load_cookies(args.cookies)
    manager = AliceAgentManager(cookie_str, cookie_dict, verbose=args.verbose)

    logger.info(f"Загрузка агентов: зарегистрировано {len(registry.list_all())} модулей.")
    await manager.initialize()

    if args.http:
        import uvicorn
        logger.info(f"Запуск curl HTTP Сервера на http://{args.host}:{args.port}")
        app = create_curl_http_app(manager)
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            await manager.shutdown()
    else:
        logger.info("Запуск Alice MCP stdio Server...")
        mcp = AliceMcpServer(manager)
        try:
            await mcp.run_stdio()
        finally:
            await manager.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Yandex Alice AI Unified Server")
    parser.add_argument("--http", action="store_true", help="Запустить HTTP сервер для curl")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parser.add_argument("--cookies", default=COOKIE_FILE_DEFAULT, help="Путь к файлу кук (.alice_cookies)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Компактный лог событий WS и RPC")
    args = parser.parse_args()

    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    if not args.http:
        logging.basicConfig(level=logging.INFO, format=log_format, stream=sys.stderr)
    else:
        logging.basicConfig(level=logging.INFO, format=log_format)

    try:
        asyncio.run(main_async(args))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()