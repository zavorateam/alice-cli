# alice_api_server.py
import asyncio
import json
import os
import sys
import uuid
import time
import argparse
from datetime import datetime
from contextlib import asynccontextmanager

import websockets
import aiohttp
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

# ---------- Конфигурация ----------
WEBSOCKET_URI = "wss://uniproxy.alice.yandex.ru/uni.ws"
RPC_URL = "https://rpc.alice.yandex.ru/dialog/remove_dialog"
ORIGIN = "https://alice.yandex.ru"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0"
COOKIE_FILE = ".alice_cookies"

# ---------- Переводы ----------
TRANSLATIONS = {
    "en": {
        "connecting": "Connecting to WebSocket...",
        "syncing": "Synchronizing state...",
        "session_set": "Session established: {id}",
        "chat_created": "Chat created: {id}",
        "chat_deleted": "Chat {id} deleted.",
        "no_active_chat": "No active chat. Creating new...",
        "ws_closed": "Connection closed: {e}. Reconnecting in 5s...",
        "cli_title": "=== CLI chat with Yandex Alice ===",
        "help_cmd": "\nAvailable commands:",
        "help_new": "  /new    (n)   Create a new chat and switch to it",
        "help_delete": "  /delete (d)   Delete chat (usage: /delete <dialog_id>)",
        "help_list": "  /list   (l)   Show list of all chats",
        "help_exit": "  /exit   (q)   Exit CLI",
        "help_other": "Any other message is sent to the active chat.\n",
        "active_chat": "Active chat: {id}",
        "all_chats": "All chats:",
        "chat_marker": " <-- active",
        "delete_usage": "Usage: /delete <dialog_id>",
        "unknown_cmd": "Unknown command. Type /help for help.",
        "alice_response": "Alice: {text}",
        "error": "Error: {e}",
        "timeout": "Timeout waiting for Alice's response",
        "no_user_msg": "No user message found",
        "dialog_not_found": "Dialog not found",
        "no_dialogs_create": "No active dialogs, creating new...",
        "delete_error": "Error deleting chat: {status} {text}",
        "internal_error": "Internal server error: {e}",
        "api_server": "Alice API server. See /docs for details.",
        "cookie_missing": f"Cookie file '{COOKIE_FILE}' not found. Run get_cookies.py.",
        "force_create": "Could not obtain active dialog. Creating forcibly.",
    },
    "ru": {
        "connecting": "Подключение к WebSocket...",
        "syncing": "Синхронизация состояния...",
        "session_set": "Сессия установлена: {id}",
        "chat_created": "Создан чат {id}",
        "chat_deleted": "Чат {id} удалён.",
        "no_active_chat": "Нет активного чата. Создаю новый...",
        "ws_closed": "Соединение разорвано: {e}. Попытка переподключения через 5с...",
        "cli_title": "=== CLI-чат с Яндекс Алисой ===",
        "help_cmd": "\nДоступные команды:",
        "help_new": "  /new    (n)   Создать новый чат и переключиться на него",
        "help_delete": "  /delete (d)   Удалить чат (использование: /delete <dialog_id>)",
        "help_list": "  /list   (l)   Показать список всех чатов",
        "help_exit": "  /exit   (q)   Выйти из CLI",
        "help_other": "Любое другое сообщение отправляется в активный чат.\n",
        "active_chat": "Активный чат: {id}",
        "all_chats": "Все чаты:",
        "chat_marker": " <-- активный",
        "delete_usage": "Использование: /delete <dialog_id>",
        "unknown_cmd": "Неизвестная команда. Введите /help для справки.",
        "alice_response": "Алиса: {text}",
        "error": "Ошибка: {e}",
        "timeout": "Таймаут ожидания ответа от Алисы",
        "no_user_msg": "Нет сообщения пользователя",
        "dialog_not_found": "Диалог не найден",
        "no_dialogs_create": "Нет активных чатов, создаю новый...",
        "delete_error": "Ошибка удаления чата: {status} {text}",
        "internal_error": "Внутренняя ошибка сервера: {e}",
        "api_server": "Alice API server. See /docs for details.",
        "cookie_missing": f"Файл '{COOKIE_FILE}' не найден. Запустите get_cookies.py.",
        "force_create": "Не удалось получить активный диалог. Создаю принудительно.",
    }
}

# ---------- Шаблоны сообщений ----------
SYNCHRONIZE_STATE_PAYLOAD = {
    "event": {
        "header": {"namespace": "System", "name": "SynchronizeState", "seqNumber": 1, "messageId": "{message_id}"},
        "payload": {
            "auth_token": "{auth_token}",
            "uuid": "{alice_uuid}",
            "vins": {"application": {"app_id": "ru.yandex.webstandalone.desktop", "platform": "windows", "device_id": "{alice_uuid}"}},
            "supported_features": ["background_response_streaming_for_dialog_controls", "background_response_streaming_in_read_dialog", "background_response_streaming_anon", "background_response_streaming", "supports_bso_answer", "open_link", "server_action", "show_promo", "reminders_and_todos", "div2_cards", "player_pause_directive", "can_open_dialogs_in_tabs", "supports_streaming_response", "supports_rich_json_cards", "builtin_reaction", "open_link_by_button", "supports_origin_in_separate_card", "supports_new_sources_cards", "supports_markdown_response", "supported_save_chathistory", "supported_load_chathistory", "supports_unlimited_dialogs_creation", "supports_multi_model_dialogs", "print_text_in_message_view", "show_loader_directive", "supports_stringbody_in_div2_card", "supports_default_dialog_as_dedicated", "whisper"],
            "request": {"experiments": ["dont_skip_cancel_requests", "enable_parallel_requests_to_chats", "read_dialogs_for_unauthorized_users", "mm_allow_anonymous_request", "enable_external_skills_for_webdesktop_and_webtouch", "send_show_view_directive_on_supports_show_view_layer_content_interface", "standalone_alice_2_0", "mm_enable_protocol_scenario=WebAliceControls", "exp_flag_chat_dialog_history", "exp_flag_chat_dialog_history_main_context_save", "div2cards_in_external_skills_for_web_standalone", "enable_find_poi_standalone", "use_server_pings", "enable_onboarding_adaptive_size", "standalone_show_fullscreen_image_gallery_directive", "draw_picture_enable_controls", "alice_has_borders_div_paddings", "enable_new_colors_for_alice_chat", "erase_serialized_response_from_json_deferred_alice_response", "skills_standalone_use_div_render", "standalone_skill_card_cloud_ui", "alice_enable_generate_video", "aliceapp_enable_generate_video", "alice_video_generation_soon", "new_input_bts"]},
            "speechkitVersion": "4.16.7",
            "icookie": "{icookie}",
            "client_analytics_info": {"client_url": "https://alice.yandex.ru/chat/"}
        }
    }
}

TEXT_INPUT_PAYLOAD = {
    "event": {
        "header": {"namespace": "Vins", "name": "TextInput", "messageId": "{message_id}", "seqNumber": 0},
        "payload": {
            "application": {"app_id": "ru.yandex.webstandalone.desktop", "app_version": "unknown", "platform": "windows", "os_version": USER_AGENT.lower(), "uuid": "{alice_uuid}", "lang": "{lang}", "client_time": "{client_time}", "timezone": "{timezone}", "timestamp": "{timestamp}"},
            "header": {"request_id": "{message_id}", "dialog_id": "{dialog_id}", "dialog_type": 2},
            "request": {
                "event": {"type": "text_input", "text": "{text}"}, "voice_session": False,
                "experiments": ["dont_skip_cancel_requests", "enable_parallel_requests_to_chats", "read_dialogs_for_unauthorized_users", "mm_allow_anonymous_request", "enable_external_skills_for_webdesktop_and_webtouch", "send_show_view_directive_on_supports_show_view_layer_content_interface", "standalone_alice_2_0", "mm_enable_protocol_scenario=WebAliceControls", "exp_flag_chat_dialog_history", "exp_flag_chat_dialog_history_main_context_save", "div2cards_in_external_skills_for_web_standalone", "enable_find_poi_standalone", "use_server_pings", "enable_onboarding_adaptive_size", "standalone_show_fullscreen_image_gallery_directive", "draw_picture_enable_controls", "alice_has_borders_div_paddings", "enable_new_colors_for_alice_chat", "erase_serialized_response_from_json_deferred_alice_response", "skills_standalone_use_div_render", "standalone_skill_card_cloud_ui", "alice_enable_generate_video", "aliceapp_enable_generate_video", "alice_video_generation_soon", "new_input_bts"],
                "additional_options": {"bass_options": {"user_agent": USER_AGENT}, "origin_domain": "yandex.ru", "supported_features": SYNCHRONIZE_STATE_PAYLOAD["event"]["payload"]["supported_features"], "icookie": "{icookie}"},
                "environment_state": {"endpoints": [{"id": "{alice_uuid}", "capabilities": [{"@type": "type.googleapis.com/NAlice.TAliceCapability"}, {"@type": "type.googleapis.com/NAlice.TAliceChatCapability", "parameters": {"supports_rich_answers": True, "supports_rich_suggests": True, "supports_rich_summary": True}, "state": {"active_chat_dialog_context": {"dialog_id": "{dialog_id}", "dialog_type": "DEDICATED_CHAT", "alice2_mode_info": {"preset": "", "mode": "Pro"}}}}]}]}
            }
        }
    }
}

# ---------- Улучшенный класс AliceClient ----------
class AliceClient:
    def __init__(self, language="en"):
        self.lang = language
        self.t = TRANSLATIONS[language]
        self.cookies = self._load_cookies()
        self.uuid = self._get_uuid()
        self.icookie = self._get_icookie()
        self.session_id = None
        self.seq_number = 0
        self.dialogs = set()
        self.active_dialog_id = None
        self.response_events = {}
        # Для запросов всегда используем русскую локаль
        self.api_lang = "ru-RU"
        self.api_timezone = "Europe/Moscow"

    def _load_cookies(self):
        if not os.path.exists(COOKIE_FILE):
            raise FileNotFoundError(self.t["cookie_missing"])
        with open(COOKIE_FILE, "r") as f:
            return f.read().strip()

    def _get_uuid(self):
        for cookie in self.cookies.split('; '):
            if cookie.startswith('alice_uuid='):
                return cookie.split('=', 1)[1]
        return "00000000000003022842051748950175"

    def _get_icookie(self):
        for cookie in self.cookies.split('; '):
            if cookie.startswith('i='):
                return cookie.split('=', 1)[1]
        return ""

    async def _send_json(self, data):
        self.seq_number += 1
        if 'header' in data.get('event', {}):
            data['event']['header']['seqNumber'] = self.seq_number
        await self.websocket.send(json.dumps(data, ensure_ascii=False))

    async def _handle_pong(self, ping_message):
        ref_id = ping_message['directive']['header']['messageId']
        payload = {"event": {"header": {"namespace": "System", "name": "Pong", "messageId": str(uuid.uuid4()), "refMessageId": ref_id}, "payload": {}}}
        await self._send_json(payload)

    async def connect(self):
        headers = {"User-Agent": USER_AGENT, "Origin": ORIGIN, "Cookie": self.cookies}
        print(self.t["connecting"])
        connection = websockets.connect(WEBSOCKET_URI, ping_interval=None)
        connection.extra_headers = headers
        self.websocket = await connection
        print(self.t["syncing"])
        await self._synchronize_state()
        asyncio.create_task(self.listen())

    async def _synchronize_state(self):
        payload_str = json.dumps(SYNCHRONIZE_STATE_PAYLOAD)
        payload_str = payload_str.replace("{message_id}", str(uuid.uuid4()))
        payload_str = payload_str.replace("{auth_token}", str(uuid.uuid4()))
        payload_str = payload_str.replace("{alice_uuid}", self.uuid)
        payload_str = payload_str.replace("{icookie}", self.icookie)
        await self._send_json(json.loads(payload_str))

    async def disconnect(self):
        if self.websocket:
            await self.websocket.close()
            print("WebSocket closed.")


    async def create_new_chat(self):
        new_dialog_id = str(uuid.uuid4())
        self.dialogs.add(new_dialog_id)
        self.active_dialog_id = new_dialog_id

        subscriptions = [{"id": did, "state": {"ping": {}}} for did in self.dialogs if did != new_dialog_id]
        subscriptions.append({"id": new_dialog_id, "state": {"full_content": {}}})

        payload = {
            "event": {
                "header": {"namespace": "System", "name": "ClientSubscriptionState", "messageId": str(uuid.uuid4())},
                "payload": {"subscriptions": subscriptions}
            }
        }
        await self._send_json(payload)
        print(self.t["chat_created"].format(id=new_dialog_id))
        return new_dialog_id

    async def delete_chat(self, dialog_id: str):
        if dialog_id not in self.dialogs:
            raise ValueError(self.t["dialog_not_found"])

        headers = {
            "User-Agent": USER_AGENT,
            "Origin": ORIGIN,
            "Cookie": self.cookies,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Ya-App-Id": "ru.yandex.webstandalone.desktop",
            "X-Ya-Uuid": self.uuid,
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(RPC_URL, json={"dialog_id": dialog_id}) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(self.t["delete_error"].format(status=resp.status, text=text))

        self.dialogs.remove(dialog_id)
        if self.active_dialog_id == dialog_id:
            self.active_dialog_id = next(iter(self.dialogs), None)

        subscriptions = [{"id": did, "state": {"ping": {}}} for did in self.dialogs]
        payload = {
            "event": {"header": {"namespace": "System", "name": "ClientSubscriptionState", "messageId": str(uuid.uuid4())}, "payload": {"subscriptions": subscriptions}}
        }
        await self._send_json(payload)
        return {"status": "success", "deleted_dialog_id": dialog_id}

    async def send_text(self, text: str, dialog_id: str):
        if dialog_id not in self.dialogs:
            raise ValueError(self.t["dialog_not_found"])

        message_id = str(uuid.uuid4())
        payload_str = json.dumps(TEXT_INPUT_PAYLOAD)
        replacements = {
            "{message_id}": message_id,
            "{dialog_id}": dialog_id,
            "{text}": text,
            "{client_time}": datetime.now().strftime("%Y%m%dT%H%M%S"),
            "{timestamp}": str(int(time.time() * 1000)),
            "{icookie}": self.icookie,
            "{alice_uuid}": self.uuid,
            "{lang}": self.api_lang,          # всегда ru-RU
            "{timezone}": self.api_timezone,  # всегда Europe/Moscow
        }
        for key, val in replacements.items():
            payload_str = payload_str.replace(key, val)

        event = asyncio.Event()
        self.response_events[message_id] = (event, "")
        await self._send_json(json.loads(payload_str))

        try:
            await asyncio.wait_for(event.wait(), timeout=30.0)
            return self.response_events.pop(message_id)[1]
        except asyncio.TimeoutError:
            self.response_events.pop(message_id, None)
            raise HTTPException(status_code=504, detail=self.t["timeout"])

    async def listen(self):
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    if 'directive' not in data:
                        continue
                    header = data['directive']['header']
                    name = header.get('name')
                    ref_id = header.get('refMessageId')

                    if name == "SynchronizeStateResponse":
                        self.session_id = data['directive']['payload']['SessionId']
                        print(self.t["session_set"].format(id=self.session_id))
                        await self.create_new_chat()
                    elif name == "Ping":
                        await self._handle_pong(data)
                    elif name == "DeferredAliceResponse" and ref_id in self.response_events:
                        payload = data['directive']['payload']
                        if 'json_response' in payload and 'base_response' in payload['json_response']:
                            text = payload['json_response']['base_response'].get('text', '')
                            if text:
                                event, _ = self.response_events[ref_id]
                                self.response_events[ref_id] = (event, text)
                            if payload['json_response'].get('is_last'):
                                self.response_events[ref_id][0].set()
                except Exception as e:
                    # Логируем ошибку парсинга, но не роняем соединение
                    print(f"Error processing message: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            print(self.t["ws_closed"].format(e=e))
            await asyncio.sleep(5)
            await self.connect()

# ---------- FastAPI приложение ----------
app = FastAPI()

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "yandex-alice"
    messages: List[ChatMessage]
    dialog_id: Optional[str] = None

class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"

class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: "chatcmpl-" + str(uuid.uuid4()))
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "yandex-alice"
    choices: List[ChatCompletionChoice]

alice_client: Optional[AliceClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global alice_client
    lang = getattr(app.state, "language", "en")
    alice_client = AliceClient(language=lang)
    await alice_client.connect()
    yield
    await alice_client.disconnect()

app.router.lifespan_context = lifespan

@app.get("/")
def root():
    t = alice_client.t if alice_client else TRANSLATIONS["en"]
    return {"message": t["api_server"]}

@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest):
    t = alice_client.t
    user_msg = next((m.content for m in reversed(request.messages) if m.role == "user"), None)
    if not user_msg:
        raise HTTPException(status_code=400, detail=t["no_user_msg"])

    dialog_id = request.dialog_id or alice_client.active_dialog_id
    if not dialog_id:
        print(t["no_dialogs_create"])
        dialog_id = await alice_client.create_new_chat()
        await asyncio.sleep(1)

    try:
        response_text = await alice_client.send_text(user_msg, dialog_id)
        return ChatCompletionResponse(choices=[ChatCompletionChoice(message=ChatMessage(role="assistant", content=response_text))])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/dialogs")
async def list_dialogs():
    return {"active_dialog_id": alice_client.active_dialog_id, "all_dialogs": list(alice_client.dialogs)}

@app.post("/dialogs")
async def create_dialog():
    new_id = await alice_client.create_new_chat()
    return {"status": "success", "new_dialog_id": new_id}

@app.delete("/dialogs/{dialog_id}")
async def delete_dialog(dialog_id: str):
    try:
        result = await alice_client.delete_chat(dialog_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=alice_client.t["internal_error"].format(e=e))

# ---------- CLI ----------
async def run_cli(language="en"):
    t = TRANSLATIONS[language]
    print(t["cli_title"])
    client = AliceClient(language=language)
    try:
        await client.connect()
        for _ in range(50):
            if client.active_dialog_id:
                break
            await asyncio.sleep(0.2)
        if not client.active_dialog_id:
            print(t["force_create"])
            await client.create_new_chat()

        print(t["help_cmd"])
        print(t["help_new"])
        print(t["help_delete"])
        print(t["help_list"])
        print(t["help_exit"])
        print(t["help_other"])

        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(None, input, "> ")
            except (EOFError, KeyboardInterrupt):
                break

            cmd = user_input.strip()
            if not cmd:
                continue

            if cmd.startswith("/"):
                parts = cmd.split(maxsplit=1)
                command = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if command in ("/help", "/h"):
                    print(t["help_cmd"])
                    print(t["help_new"])
                    print(t["help_delete"])
                    print(t["help_list"])
                    print(t["help_exit"])
                    print(t["help_other"])
                elif command in ("/list", "/l"):
                    print(t["active_chat"].format(id=client.active_dialog_id))
                    print(t["all_chats"])
                    for d in client.dialogs:
                        marker = t["chat_marker"] if d == client.active_dialog_id else ""
                        print(f"  {d}{marker}")
                elif command in ("/new", "/n"):
                    await client.create_new_chat()
                elif command in ("/delete", "/d"):
                    if not arg:
                        print(t["delete_usage"])
                    else:
                        try:
                            res = await client.delete_chat(arg)
                            print(t["chat_deleted"].format(id=res["deleted_dialog_id"]))
                        except Exception as e:
                            print(t["error"].format(e=e))
                elif command in ("/exit", "/q"):
                    break
                else:
                    print(t["unknown_cmd"])
            else:
                if not client.active_dialog_id:
                    print(t["no_active_chat"])
                    await client.create_new_chat()
                try:
                    response = await client.send_text(cmd, client.active_dialog_id)
                    print(t["alice_response"].format(text=response))
                except Exception as e:
                    print(t["error"].format(e=e))
    finally:
        await client.disconnect()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Yandex Alice API server & CLI")
    parser.add_argument("--cli", action="store_true", help="Run interactive CLI mode")
    parser.add_argument("--lang", default="en", choices=["en", "ru"], help="Language (default: en)")
    args = parser.parse_args()

    if args.cli:
        asyncio.run(run_cli(language=args.lang))
    else:
        app.state.language = args.lang
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8000)