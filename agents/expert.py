"""
Экспертная модель. Отдельный preset от Pro.
Trial-лимит ~5 запросов / 24 ч (см. trial_state в VinsResponse).

Реальный браузер для Expert шлёт:
  - FEATS = 28 (не 13 как Pro)
  - EXPS  = 21 (с *_unauthorized / *_anonymous)
  - 11 capabilities, включая TJeevesCapability (инструменты эксперта
    на стороне сервера) и TAliceChatCapability с 18 директивами.

Эти константы и build_* методы — ТОЛЬКО для Expert.
Pro/Base/Apps используют DEFAULT_* из alice_server.py.
"""
from typing import Any, Dict, List

from agents.base import AliceAgent, AliceTurnResponse


# ── FEATS: 28, как шлёт реальный браузер для Expert ──────────────────────
EXPERT_SUPPORTED_FEATURES: List[str] = [
    "background_response_streaming_for_dialog_controls",
    "background_response_streaming_in_read_dialog",
    "background_response_streaming",
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
    "background_response_streaming_anon",
    "whisper",
]

# ── EXPS: 21, как шлёт реальный браузер для Expert ───────────────────────
EXPERT_EXPERIMENTS: List[str] = [
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

# ── Директивы TAliceChatCapability: 18, как у реального браузера ────────
EXPERT_CHAT_DIRECTIVES: List[str] = [
    "ScrollToFragmentDirectiveType",
    "LoginWithCallbackDirectiveType",
    "LimitExceededBannerDirectiveType",
    "FewRequestsLeftBannerDirective",
    "ShowBannerDirectiveType",
    "ShowProPurchaseScreenDirectiveType",
    "ShowModalDirectiveType",
    "ShowNotificationDirectiveType",
    "SetChatRequestModeDirectiveType",
    "ShowFullscreenImageGalleryDirectiveType",
    "ZoomImageDirectiveType",
    "DownloadImageDirectiveType",
    "FillChatInputDirectiveType",
    "FinishChatDirectiveType",
    "CreateNewChatDirectiveType",
    "OpenChatListDirectiveType",
    "ChatNavigateBackDirectiveType",
    "SuggestReaskDirectiveType",
]


class ExpertAgent(AliceAgent):
    id = "expert"
    name = "Эксперт"
    aliases = ["эксперт", "expert", "rn_expert", "rnservicealiceexpert"]
    type = "model"
    mode = "External"
    preset = "agents_RnServiceAliceExpert"
    app_id = None
    description = "Экспертная модель с длинными рассуждениями. Trial ~5 запросов/24ч."
    timeout = 300.0  # Эксперт медленнее

    # ── Настройки для TextInput (alice_server._resolve_*) ────────────────

    def build_experiments(self) -> List[str]:
        """Expert-специфичные experiments (21). Pro/Base — DEFAULT_* в server."""
        return list(EXPERT_EXPERIMENTS)

    def build_supported_features(self) -> List[str]:
        """Expert-специфичные FEATS (28). Pro/Base — DEFAULT_* в server."""
        return list(EXPERT_SUPPORTED_FEATURES)

    def build_capabilities(self, dialog_id: str, uuid: str) -> List[Dict[str, Any]]:
        """
        11 capabilities — ровно как у реального браузера для Expert.
        TJeevesCapability (enabled_plugin_ids=[]) активирует инструменты
        эксперта на стороне сервера (shell, веб-поиск, python и т.д.).
        Без этой capability сервер отвечает как обычный чат с preset'ом
        agents_RnServiceAliceExpert, но БЕЗ своих skill'ов.
        """
        return [
            # 1. Базовая
            {
                "@type": "type.googleapis.com/NAlice.TAliceCapability",
                "meta": {}, "parameters": {}, "state": {},
            },
            # 2. Chat (с 18 директивами, а не 3 как у Pro)
            {
                "@type": "type.googleapis.com/NAlice.TAliceChatCapability",
                "meta": {"supported_directives": list(EXPERT_CHAT_DIRECTIVES)},
                "parameters": {
                    "supports_rich_answers": True,
                    "supports_rich_suggests": True,
                    "supports_rich_summary": True,
                },
                "state": {
                    "navigation_state": {
                        "topmost_navigation_entry": {
                            "id": "chat", "type": "Chat",
                            "title": "Чат с Алисой",
                            "can_navigate_back": False,
                        }
                    },
                    "active_chat_dialog_context": {
                        "dialog_id": dialog_id,
                        "dialog_type": "DEDICATED_CHAT",
                        "alice2_mode_info": {
                            "preset": "agents_RnServiceAliceExpert",
                            "mode": "External",
                        },
                    },
                },
            },
            # 3. Pro-подписка
            {
                "@type": "type.googleapis.com/NAlice.TAliceProCapability",
                "meta": {"supported_directives": ["OpenProPurchaseScreenDirectiveType"]},
                "parameters": {}, "state": {},
            },
            # 4. Best-price
            {
                "@type": "type.googleapis.com/NAlice.TBestPriceChatCapability",
                "meta": {}, "state": {},
            },
            # 5. Div-view
            {
                "@type": "type.googleapis.com/NAlice.TDivViewCapability",
                "meta": {}, "parameters": {}, "state": {},
            },
            # 6. Personal device
            {
                "@type": "type.googleapis.com/NAlice.TPersonalDeviceCapability",
                "meta": {}, "parameters": {}, "state": {},
            },
            # 7. Retrieve sources (источники в ответе)
            {
                "@type": "type.googleapis.com/NAlice.TRetrieveSourcesCapability",
                "meta": {}, "parameters": {"sources": []},
            },
            # 8. Alice limits (trial_state эксперта)
            {
                "@type": "type.googleapis.com/NAlice.TAliceLimitsCapability",
                "meta": {"supported_directives": [
                    "ExpertLimitNoticeDirectiveType",
                    "LimitUpdatedDirectiveType",
                ]},
                "parameters": {}, "state": {},
            },
            # 9. Apps widget
            {
                "@type": "type.googleapis.com/NAlice.TAliceAppsWidgetCapability",
                "meta": {}, "parameters": {},
                "state": {"widget_states": []},
            },
            # 10. Apps (приложения)
            {
                "@type": "type.googleapis.com/NAlice.TAliceAppsCapability",
                "meta": {"supported_directives": ["AliceAppsSelectDirectiveType"]},
                "parameters": {}, "state": {},
            },
            # 11. Jeeves — ЭТО активирует инструменты Expert на сервере.
            #     enabled_plugin_ids=[] означает "включи стандартный набор".
            {
                "@type": "type.googleapis.com/NAlice.TJeevesCapability",
                "meta": {}, "parameters": {},
                "state": {"enabled_plugin_ids": []},
            },
        ]

    # ── Парсер ответов ───────────────────────────────────────────────────

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        tc = card.get("text_card")
        if tc and "text" in tc:
            t = tc["text"].strip()
            if t and t not in text_blocks:
                text_blocks.append(t)
            return True
        return False