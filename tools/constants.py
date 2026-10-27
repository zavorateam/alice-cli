"""Константы протокола Alice и утилита uuid7."""

import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

WEBSOCKET_URI = "wss://uniproxy.alice.yandex.ru/uni.ws"

RPC_BASE_URL = "https://rpc.alice.yandex.ru"

ORIGIN = "https://alice.yandex.ru"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"

TEXTINPUT_UA = "Mozilla/5.0"

DEFAULT_EXPERIMENTS = [
    "dont_skip_cancel_requests",
    "enable_parallel_requests_to_chats",
    "standalone_alice_2_0",
    "mm_enable_protocol_scenario=WebAliceControls",
    "exp_flag_chat_dialog_history",
    "exp_flag_chat_dialog_history_main_context_save",
    "div2cards_in_external_skills_for_web_standalone",
    "use_server_pings",
    "erase_serialized_response_from_json_deferred_alice_response",
    "skills_standalone_use_div_render",
    "standalone_skill_card_cloud_ui",
]

DEFAULT_SUPPORTED_FEATURES = [
    "background_response_streaming",
    "supports_bso_answer",
    "open_link",
    "server_action",
    "div2_cards",
    "supports_streaming_response",
    "supports_rich_json_cards",
    "supports_markdown_response",
    "supported_save_chathistory",
    "supports_multi_model_dialogs",
    "print_text_in_message_view",
    "supports_unlimited_dialogs_creation",
    "supports_default_dialog_as_dedicated",
]

_FRAME_SEQ: Dict[Tuple[str, str], int] = {
    ("System", "SynchronizeState"): 1,
    ("System", "ClientSubscriptionState"): 2,
    ("Vins", "TextInput"): 3,
}


def uuid7() -> str:
    ms = int(time.time() * 1000)
    b = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    h = bytes(b).hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
