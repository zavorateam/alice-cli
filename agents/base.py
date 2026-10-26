#!/usr/bin/env python3
"""
Базовые классы: ThinkingStep (структурированный trace), AliceTurnResponse с thinking_steps + sources.
"""

import json
import urllib.parse
from abc import ABC
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ThinkingStep:
    """
    Один шаг рассуждения/секции из neuro_structured_blocks_card.
    kind:
      - "reasoning_title"   — заголовок блока рассуждений
      - "reasoning_content" — детали рассуждений
      - "tool"              — вызов инструмента (Python, WebSearch, File, ...)
    """
    kind: str
    title: Optional[str] = None
    content: Optional[str] = None
    tool: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    model_dump = to_dict


@dataclass
class SuggestionAction:
    title: str
    action_type: str = "text"
    payload: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    model_dump = to_dict


@dataclass
class AgentWidget:
    component_name: str
    app_id: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    model_dump = to_dict


@dataclass
class AliceTurnResponse:
    dialog_id: str
    request_id: str
    thinking_logs: List[str] = field(default_factory=list)
    thinking_steps: List[ThinkingStep] = field(default_factory=list)
    text: str = ""
    widgets: List[AgentWidget] = field(default_factory=list)
    suggestions: List[SuggestionAction] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    is_finished: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dialog_id": self.dialog_id,
            "request_id": self.request_id,
            "thinking_logs": self.thinking_logs,
            "thinking_steps": [s.to_dict() for s in self.thinking_steps],
            "text": self.text,
            "widgets": [w.to_dict() for w in self.widgets],
            "suggestions": [s.to_dict() for s in self.suggestions],
            "sources": self.sources,
            "is_finished": self.is_finished,
            "error": self.error,
        }
    model_dump = to_dict


def decode_dialog_action(payload_str: str) -> str:
    if not payload_str or not payload_str.startswith("dialog-action://"):
        return payload_str
    try:
        parsed = urllib.parse.urlparse(payload_str)
        qs = urllib.parse.parse_qs(parsed.query)
        if "directives" in qs:
            dirs = json.loads(qs["directives"][0])
            for d in dirs:
                t = d.get("payload", {}).get("text")
                if t:
                    return t
    except Exception:
        pass
    return payload_str


class AliceAgent(ABC):
    id: str = ""
    name: str = ""
    aliases: List[str] = []
    type: str = "agent"
    mode: str = "External"
    preset: str = ""
    app_id: Optional[str] = None
    description: str = ""
    timeout: float = 60.0

    def build_capabilities(self, dialog_id: str, device_uuid: str) -> List[Dict[str, Any]]:
        return [
            {"@type": "type.googleapis.com/NAlice.TAliceCapability", "meta": {}, "parameters": {}, "state": {}},
            {
                "@type": "type.googleapis.com/NAlice.TAliceChatCapability",
                "meta": {
                    "supported_directives": [
                        "ScrollToFragmentDirectiveType", "LoginWithCallbackDirectiveType",
                        "LimitExceededBannerDirectiveType", "FewRequestsLeftBannerDirective",
                        "ShowBannerDirectiveType", "ShowProPurchaseScreenDirectiveType",
                        "ShowModalDirectiveType", "ShowNotificationDirectiveType",
                        "SetChatRequestModeDirectiveType", "ShowFullscreenImageGalleryDirectiveType",
                        "ZoomImageDirectiveType", "DownloadImageDirectiveType",
                        "FillChatInputDirectiveType", "FinishChatDirectiveType",
                        "CreateNewChatDirectiveType", "OpenChatListDirectiveType",
                        "ChatNavigateBackDirectiveType", "SuggestReaskDirectiveType",
                    ]
                },
                "parameters": {
                    "supports_rich_answers": True,
                    "supports_rich_suggests": True,
                    "supports_rich_summary": True,
                },
                "state": {
                    "navigation_state": {
                        "topmost_navigation_entry": {
                            "id": "chat", "type": "Chat", "title": "Чат с Алисой", "can_navigate_back": False
                        }
                    },
                    "active_chat_dialog_context": {
                        "dialog_id": dialog_id,
                        "dialog_type": "DEDICATED_CHAT",
                        "alice2_mode_info": {"preset": self.preset, "mode": self.mode},
                    },
                },
            },
            {"@type": "type.googleapis.com/NAlice.TAliceProCapability", "meta": {"supported_directives": ["OpenProPurchaseScreenDirectiveType"]}, "parameters": {}, "state": {}},
            {"@type": "type.googleapis.com/NAlice.TBestPriceChatCapability", "meta": {}, "state": {}},
            {"@type": "type.googleapis.com/NAlice.TDivViewCapability", "meta": {}, "parameters": {}, "state": {}},
            {"@type": "type.googleapis.com/NAlice.TPersonalDeviceCapability", "meta": {}, "parameters": {}, "state": {}},
            {"@type": "type.googleapis.com/NAlice.TRetrieveSourcesCapability", "meta": {}, "parameters": {"sources": []}},
            {"@type": "type.googleapis.com/NAlice.TAliceLimitsCapability", "meta": {"supported_directives": ["ExpertLimitNoticeDirectiveType", "LimitUpdatedDirectiveType"]}, "parameters": {}, "state": {}},
            {"@type": "type.googleapis.com/NAlice.TAliceAppsWidgetCapability", "meta": {}, "parameters": {}, "state": {"widget_states": []}},
            {
                "@type": "type.googleapis.com/NAlice.TAliceAppsCapability",
                "meta": {"supported_directives": ["AliceAppsSelectDirectiveType"]},
                "parameters": {},
                "state": {"selected_app_id": self.app_id} if self.app_id else {},
            },
            {"@type": "type.googleapis.com/NAlice.TJeevesCapability", "meta": {}, "parameters": {}, "state": {"enabled_plugin_ids": []}},
        ]

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        return False