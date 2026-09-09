from typing import Any, Dict, List
from agents.base import AliceAgent, AliceTurnResponse


class ProAgent(AliceAgent):
    id = "pro"
    name = "Продвинутый режим (Pro)"
    aliases = ["про", "pro", "gpt", "yagpt_pro"]
    type = "model"
    mode = "Pro"
    preset = ""
    app_id = None
    description = "Развернутые ответы и продвинутый tool use для подписчиков Алиса Плюс"
    timeout = 60.0

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        tc = card.get("text_card")
        if tc and "text" in tc:
            t = tc["text"].strip()
            if t and t not in text_blocks:
                text_blocks.append(t)
            return True
        return False