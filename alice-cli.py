# alice_cli.py
import asyncio
import json
import os
import sys
import uuid
import time
import argparse
from datetime import datetime
import websockets

# ---------- Конфигурация ----------
WEBSOCKET_URI = "wss://uniproxy.alice.yandex.ru/uni.ws"
RPC_URL = "https://rpc.alice.yandex.ru/dialog/remove_dialog"
ORIGIN = "https://alice.yandex.ru"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0"
COOKIE_FILE = ".alice_cookies"

# Переводы
TRANSLATIONS = {
    "en": {
        "connecting": "Connecting to Alice...",
        "syncing": "Syncing state...",
        "session_set": "Session OK.",
        "chat_created": "Chat created.",
        "chat_deleted": "Chat removed.",
        "cli_title": "=== Alice CLI ===",
        "help_line": "Type /exit to leave.\n",
        "alice_response": "Alice: {text}",
        "error": "Error: {e}",
        "timeout": "No response from Alice (timeout).",
        "dialog_not_found": "Chat not found.",
        "delete_error": "Failed to delete chat: {status} {text}",
        "cookie_missing": f"Cookie file '{COOKIE_FILE}' not found. Run get_cookies.py.",
        "force_create": "Creating new chat...",
        "exit_msg": "Goodbye!",
    },
    "ru": {
        "connecting": "Подключение к Алисе...",
        "syncing": "Синхронизация...",
        "session_set": "Сессия установлена.",
        "chat_created": "Чат создан.",
        "chat_deleted": "Чат удалён.",
        "cli_title": "=== Алиса CLI ===",
        "help_line": "Введите /exit для выхода.\n",
        "alice_response": "Алиса: {text}",
        "error": "Ошибка: {e}",
        "timeout": "Алиса не ответила (таймаут).",
        "dialog_not_found": "Чат не найден.",
        "delete_error": "Ошибка удаления чата: {status} {text}",
        "cookie_missing": f"Файл '{COOKIE_FILE}' не найден. Запустите get_cookies.py.",
        "force_create": "Создаю новый чат...",
        "exit_msg": "До свидания!",
    }
}

# Шаблоны сообщений (UUID будет подставляться динамически)
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
            "application": {"app_id": "ru.yandex.webstandalone.desktop", "app_version": "unknown", "platform": "windows", "os_version": USER_AGENT.lower(), "uuid": "{alice_uuid}", "lang": "ru-RU", "client_time": "{client_time}", "timezone": "Europe/Moscow", "timestamp": "{timestamp}"},
            "header": {"request_id": "{message_id}", "dialog_id": "{dialog_id}", "dialog_type": 2},
            "request": {
                "event": {"type": "text_input", "text": "{text}"},
                "voice_session": False,
                "experiments": ["dont_skip_cancel_requests", "enable_parallel_requests_to_chats", "read_dialogs_for_unauthorized_users", "mm_allow_anonymous_request", "enable_external_skills_for_webdesktop_and_webtouch", "send_show_view_directive_on_supports_show_view_layer_content_interface", "standalone_alice_2_0", "mm_enable_protocol_scenario=WebAliceControls", "exp_flag_chat_dialog_history", "exp_flag_chat_dialog_history_main_context_save", "div2cards_in_external_skills_for_web_standalone", "enable_find_poi_standalone", "use_server_pings", "enable_onboarding_adaptive_size", "standalone_show_fullscreen_image_gallery_directive", "draw_picture_enable_controls", "alice_has_borders_div_paddings", "enable_new_colors_for_alice_chat", "erase_serialized_response_from_json_deferred_alice_response", "skills_standalone_use_div_render", "standalone_skill_card_cloud_ui", "alice_enable_generate_video", "aliceapp_enable_generate_video", "alice_video_generation_soon", "new_input_bts"],
                "additional_options": {
                    "bass_options": {"user_agent": USER_AGENT},
                    "origin_domain": "yandex.ru",
                    "supported_features": SYNCHRONIZE_STATE_PAYLOAD["event"]["payload"]["supported_features"],
                    "icookie": "{icookie}"
                },
                "environment_state": {"endpoints": [{"id": "{alice_uuid}", "capabilities": [{"@type": "type.googleapis.com/NAlice.TAliceCapability"}, {"@type": "type.googleapis.com/NAlice.TAliceChatCapability", "parameters": {"supports_rich_answers": True, "supports_rich_suggests": True, "supports_rich_summary": True}, "state": {"active_chat_dialog_context": {"dialog_id": "{dialog_id}", "dialog_type": "DEDICATED_CHAT", "alice2_mode_info": {"preset": "", "mode": "Pro"}}}}]}]}
            }
        }
    }
}


class AliceClient:
    def __init__(self, language="en"):
        self.lang = language
        self.t = TRANSLATIONS[language]
        self.cookies = self._load_cookies()
        self.uuid = self._get_uuid()
        self.icookie = self._get_icookie()
        self.websocket = None
        self.session_id = None
        self.seq_number = 0
        self.active_dialog_id = None
        self.response_events = {}  # message_id -> (Event, response_text)

    def _load_cookies(self):
        if not os.path.exists(COOKIE_FILE):
            print(self.t["cookie_missing"])
            sys.exit(1)
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
        payload = {
            "event": {
                "header": {"namespace": "System", "name": "Pong", "messageId": str(uuid.uuid4()), "refMessageId": ref_id},
                "payload": {}
            }
        }
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

    async def create_chat(self):
        """Создаёт новый чат и делает его активным."""
        new_id = str(uuid.uuid4())
        self.active_dialog_id = new_id
        subscriptions = [{"id": new_id, "state": {"full_content": {}}}]
        payload = {
            "event": {
                "header": {"namespace": "System", "name": "ClientSubscriptionState", "messageId": str(uuid.uuid4())},
                "payload": {"subscriptions": subscriptions}
            }
        }
        await self._send_json(payload)
        print(self.t["chat_created"])

    async def delete_current_chat(self):
        """Удаляет текущий активный чат через HTTP и WebSocket."""
        if not self.active_dialog_id:
            return
        dialog_id = self.active_dialog_id
        try:
            import aiohttp
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
                        print(self.t["delete_error"].format(status=resp.status, text=text))
                        return
            # Обновляем подписки (пустой список)
            payload = {
                "event": {
                    "header": {"namespace": "System", "name": "ClientSubscriptionState", "messageId": str(uuid.uuid4())},
                    "payload": {"subscriptions": []}
                }
            }
            await self._send_json(payload)
            print(self.t["chat_deleted"])
        except ImportError:
            print("Install aiohttp to auto-delete chats: pip install aiohttp")
        except Exception as e:
            print(self.t["error"].format(e=e))
        finally:
            self.active_dialog_id = None

    async def send_text(self, text: str):
        if not self.active_dialog_id:
            raise ValueError(self.t["dialog_not_found"])

        message_id = str(uuid.uuid4())
        payload_str = json.dumps(TEXT_INPUT_PAYLOAD)
        replacements = {
            "{message_id}": message_id,
            "{dialog_id}": self.active_dialog_id,
            "{text}": text,
            "{client_time}": datetime.now().strftime("%Y%m%dT%H%M%S"),
            "{timestamp}": str(int(time.time() * 1000)),
            "{icookie}": self.icookie,
            "{alice_uuid}": self.uuid,
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
            raise Exception(self.t["timeout"])

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
                        print(self.t["session_set"])
                        # Создаём чат автоматически после синхронизации
                        if not self.active_dialog_id:
                            await self.create_chat()
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
                    print(f"Processing error: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            print(self.t["error"].format(e=e))
            await asyncio.sleep(5)
            await self.connect()

    async def run(self):
        """Основной цикл ввода."""
        print(self.t["help_line"])
        while True:
            try:
                line = await asyncio.get_event_loop().run_in_executor(None, input, "> ")
            except (EOFError, KeyboardInterrupt):
                break

            cmd = line.strip()
            if cmd == "/exit":
                break
            elif cmd:
                try:
                    if not self.active_dialog_id:
                        print(self.t["force_create"])
                        await self.create_chat()
                    response = await self.send_text(cmd)
                    print(self.t["alice_response"].format(text=response))
                except Exception as e:
                    print(self.t["error"].format(e=e))
        # Удаляем чат при выходе
        await self.delete_current_chat()
        print(self.t["exit_msg"])


async def main():
    parser = argparse.ArgumentParser(description="Alice CLI chat")
    parser.add_argument("--lang", default="en", choices=["en", "ru"], help="Interface language")
    args = parser.parse_args()

    client = AliceClient(language=args.lang)
    print(client.t["cli_title"])
    await client.connect()
    # Ждём создания первого чата
    for _ in range(50):
        if client.active_dialog_id:
            break
        await asyncio.sleep(0.2)
    if not client.active_dialog_id:
        print(client.t["force_create"])
        await client.create_chat()
    try:
        await client.run()
    finally:
        if client.websocket:
            await client.websocket.close()

if __name__ == "__main__":
    asyncio.run(main())