from typing import Any, Dict, List
from agents.base import AliceAgent, AliceTurnResponse


class BaseModelAgent(AliceAgent):
    id = "base"
    name = "Базовый режим"
    aliases = ["базовый", "base", "обычный"]
    type = "model"
    mode = "Base"
    preset = ""
    app_id = None
    description = "Быстрый режим для повседневных вопросов"
    timeout = 45.0

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        tc = card.get("text_card")
        if tc and "text" in tc:
            t = tc["text"].strip()
            if t and t not in text_blocks:
                text_blocks.append(t)
            return True
        return False