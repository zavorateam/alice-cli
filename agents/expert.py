"""
Экспертная модель. Отдельный preset от Pro.
Trial-лимит ~5 запросов / 24 ч (см. trial_state в VinsResponse).
"""
from typing import Any, Dict, List

from agents.base import AliceAgent, AliceTurnResponse


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

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        tc = card.get("text_card")
        if tc and "text" in tc:
            t = tc["text"].strip()
            if t and t not in text_blocks:
                text_blocks.append(t)
            return True
        return False