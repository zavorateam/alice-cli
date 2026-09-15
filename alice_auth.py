#!/usr/bin/env python3
"""
Alice Auth & Healthcheck Module (Fixed).
Содержит полный набор x-ya заголовков, синхронизацию alice_uuid и фильтрацию кук.
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger("alice_auth")

COOKIE_THRESHOLD_BYTES = 300
DEFAULT_DEVICE_UUID_FILE = ".alice_device_uuid"
DEFAULT_TOKEN_CANDIDATES = ["token.txt", ".alice_token"]
DEFAULT_COOKIE_CANDIDATES = [".alice_cookies", "cookies.txt"]

PROBE_URL = "https://rpc.alice.yandex.ru/gproxy/get_alice_apps"
ORIGIN = "https://alice.yandex.ru"
REFERER = "https://alice.yandex.ru/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0"

# Критический набор кук для авторизации
ESSENTIAL_COOKIE_KEYS = {
    "Session_id",
    "sessionid2",
    "i",
    "yandexuid",
    "alice_uuid",
    "_yasc",
    "L",
    "yp",
    "ys",
    "yandex_login",
}

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


@dataclass
class AliceCredentials:
    auth_type: str  # "token" | "cookies"
    token: Optional[str] = None
    cookie_header: Optional[str] = None
    cookie_dict: Dict[str, str] = field(default_factory=dict)
    device_uuid: str = field(default_factory=lambda: uuid.uuid4().hex)
    icookie: str = ""

    def build_rpc_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Origin": ORIGIN,
            "Referer": REFERER,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-ya-app-id": "ru.yandex.webstandalone.desktop",
            "x-ya-app-type": "other",
            "x-ya-device-id": self.device_uuid,
            "x-ya-uuid": self.device_uuid,
            "x-ya-language": "ru",
            "x-ya-supported-features": ",".join(DEFAULT_SUPPORTED_FEATURES),
            "x-ya-experiments": json.dumps(DEFAULT_EXPERIMENTS),
            "x-ya-application": json.dumps(
                {
                    "app_id": "ru.yandex.webstandalone.desktop",
                    "uuid": self.device_uuid,
                    "device_id": self.device_uuid,
                    "lang": "ru",
                    "timezone": "Europe/Moscow",
                }
            ),
        }
        if self.auth_type == "token" and self.token:
            headers["Authorization"] = f"OAuth {self.token}"
        elif self.auth_type == "cookies" and self.cookie_header:
            headers["Cookie"] = self.cookie_header
        return headers

    def build_ws_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Origin": ORIGIN,
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "x-ya-app-id": "ru.yandex.webstandalone.desktop",
            "x-ya-app-type": "other",
            "X-Ya-Uuid": self.device_uuid,
            "X-Ya-Device-Id": self.device_uuid,
        }
        if self.auth_type == "token" and self.token:
            headers["Authorization"] = f"OAuth {self.token}"
        elif self.auth_type == "cookies" and self.cookie_header:
            headers["Cookie"] = self.cookie_header
        return headers

    # Алиасы для обратной совместимости:
    def apply_to_http_headers(self, headers: Dict[str, str]) -> None:
        headers.update(self.build_rpc_headers())

    def apply_to_ws_headers(self, headers: Dict[str, str]) -> None:
        headers.update(self.build_ws_headers())

    def get_ws_auth_token(self) -> str:
        if self.auth_type == "token" and self.token:
            return self.token
        return str(uuid.uuid4())


# --- Хранилище Device UUID с Fallback ---

def _safe_read_file(filepath: str) -> Optional[str]:
    try:
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
    except Exception:
        pass
    return None


def _safe_write_file(filepath: str, content: str) -> bool:
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except (OSError, PermissionError) as e:
        logger.warning(f"Файл {filepath} недоступен для записи ({e}). Используется in-memory fallback.")
        return False


def get_or_create_device_uuid(custom_token_file: Optional[str] = None) -> str:
    if custom_token_file:
        content = _safe_read_file(custom_token_file)
        if content:
            lines = content.splitlines()
            for line in lines[1:]:
                clean = line.strip().removeprefix("device_uuid=").strip()
                if len(clean) >= 16:
                    return clean

    saved_uuid = _safe_read_file(DEFAULT_DEVICE_UUID_FILE)
    if saved_uuid and len(saved_uuid) >= 16:
        return saved_uuid

    new_uuid = uuid.uuid4().hex
    _safe_write_file(DEFAULT_DEVICE_UUID_FILE, new_uuid)
    return new_uuid


def save_token_and_uuid(token: str, device_uuid: str, filepath: str = "token.txt") -> None:
    content = f"{token}\ndevice_uuid={device_uuid}\n"
    if not _safe_write_file(filepath, content):
        _safe_write_file(DEFAULT_DEVICE_UUID_FILE, device_uuid)


# --- Парсер и фильтр кук ---

def parse_cookies_content(content: str) -> Tuple[str, Dict[str, str]]:
    cookie_dict: Dict[str, str] = {}
    lines = content.strip().splitlines()
    is_netscape = any(l.startswith("# Netscape") or "\t" in l for l in lines)

    if is_netscape:
        for line in lines:
            line = line.strip()
            if not line or line.startswith("# HTTP") or line.startswith("# Netscape"):
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            parts = line.split("\t")
            if len(parts) >= 7:
                cookie_dict[parts[5].strip()] = parts[6].strip()
    else:
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for item in line.split(";"):
                if "=" in item:
                    k, v = item.split("=", 1)
                    cookie_dict[k.strip()] = v.strip()

    # Очищаем от мусора, оставляя критически важные
    filtered_dict = {k: v for k, v in cookie_dict.items() if k in ESSENTIAL_COOKIE_KEYS}
    # Если фильтр почему-то пустой, оставляем оригинал
    active_dict = filtered_dict if "Session_id" in filtered_dict else cookie_dict

    cookie_header = "; ".join(f"{k}={v}" for k, v in active_dict.items())
    return cookie_header, active_dict


def resolve_auth_source(source: str, explicit_uuid: Optional[str] = None) -> Optional[AliceCredentials]:
    clean_src = source.strip().strip("'\"")

    if os.path.exists(clean_src):
        size = os.path.getsize(clean_src)
        content = _safe_read_file(clean_src) or ""
        dev_uuid = explicit_uuid or get_or_create_device_uuid(clean_src)

        if size > COOKIE_THRESHOLD_BYTES or "Session_id" in content or "\t" in content:
            c_hdr, c_dict = parse_cookies_content(content)
            
            # Извлекаем и нормализуем 32-значный alice_uuid из yandexuid
            if "alice_uuid" in c_dict:
                u_id = c_dict["alice_uuid"]
            elif "yandexuid" in c_dict:
                u_id = c_dict["yandexuid"].zfill(32)
            else:
                u_id = dev_uuid

            return AliceCredentials(
                auth_type="cookies",
                cookie_header=c_hdr,
                cookie_dict=c_dict,
                device_uuid=u_id,
                icookie=c_dict.get("i", ""),
            )
        else:
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            tok = lines[0] if lines else ""
            if tok.startswith("y0_"):
                return AliceCredentials(auth_type="token", token=tok, device_uuid=dev_uuid)
            return None

    dev_uuid = explicit_uuid or get_or_create_device_uuid()

    if clean_src.startswith("y0_"):
        return AliceCredentials(auth_type="token", token=clean_src, device_uuid=dev_uuid)

    if "Session_id=" in clean_src:
        c_hdr, c_dict = parse_cookies_content(clean_src)
        u_id = c_dict.get("alice_uuid") or (c_dict["yandexuid"].zfill(32) if "yandexuid" in c_dict else dev_uuid)
        return AliceCredentials(
            auth_type="cookies",
            cookie_header=c_hdr,
            cookie_dict=c_dict,
            device_uuid=u_id,
            icookie=c_dict.get("i", ""),
        )

    return None


def show_auth_instructions():
    print(
        """
\033[1;33m[!] Авторизация не найдена.\033[0m
Для подключения используйте один из вариантов:
  \033[1m1. OAuth-токен (ya-token.site):\033[0m
     • Получите токен на: \033[36mhttps://ya-token.site/\033[0m (начинается с \033[32my0_\033[0m)
     • Сохраните в \033[33mtoken.txt\033[0m или передайте: \033[33m--token y0_...\033[0m
  \033[1m2. Cookies:\033[0m
     • Получите и сохраните файл кук через расширение "Get cookies.txt LOCALLY" или аналог в \033[33m.alice_cookies\033[0m
""",
        file=sys.stderr,
    )


def load_or_request_credentials(
    explicit_source: Optional[str] = None,
    allow_interactive: bool = True,
) -> Optional[AliceCredentials]:
    dev_uuid = get_or_create_device_uuid(explicit_source if (explicit_source and os.path.exists(explicit_source)) else None)

    if explicit_source:
        creds = resolve_auth_source(explicit_source, dev_uuid)
        if creds:
            return creds

    for env_var in ("ALICE_TOKEN", "YANDEX_TOKEN", "ALICE_COOKIES"):
        val = os.environ.get(env_var)
        if val:
            creds = resolve_auth_source(val, dev_uuid)
            if creds:
                return creds

    for tf in DEFAULT_TOKEN_CANDIDATES:
        if os.path.exists(tf):
            creds = resolve_auth_source(tf, dev_uuid)
            if creds:
                return creds

    for cf in DEFAULT_COOKIE_CANDIDATES:
        if os.path.exists(cf):
            creds = resolve_auth_source(cf, dev_uuid)
            if creds:
                return creds

    if not allow_interactive or not sys.stdin.isatty():
        return None

    show_auth_instructions()
    try:
        user_input = input("\033[1;36m>>> Введите OAuth токен (начинается с y0_): \033[0m").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nВыход.", file=sys.stderr)
        sys.exit(1)

    if not user_input.startswith("y0_"):
        print("\n\033[31m[Ошибка]: Токен должен начинаться с 'y0_'.\033[0m", file=sys.stderr)
        sys.exit(1)

    save_token_and_uuid(user_input, dev_uuid, "token.txt")
    return AliceCredentials(auth_type="token", token=user_input, device_uuid=dev_uuid)


# --- Pre-flight Healthcheck с правильными заголовками ---

async def verify_credentials(creds: AliceCredentials, timeout_sec: float = 7.0) -> Tuple[bool, str]:
    headers = creds.build_rpc_headers()

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                PROBE_URL,
                json={},
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as resp:
                status = resp.status
                body = await resp.text()

                if status == 200:
                    return True, "Авторизация подтверждена (HTTP 200 OK)"
                elif status in (401, 403):
                    return False, f"Токен или куки недействительны/отозваны (HTTP {status})"
                else:
                    return False, f"Ошибка RPC {status}: {body[:150]}"
    except asyncio.TimeoutError:
        return False, "Таймаут подключения к серверам Яндекса"
    except Exception as e:
        return False, f"Сетевая ошибка: {e}"