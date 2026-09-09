from typing import Any, Dict, List
from agents.base import AliceAgent, AliceTurnResponse


class GasAgent(AliceAgent):
    id = "gas"
    name = "Найти АЗС"
    aliases = ["азс", "gas", "заправки", "бензин"]
    type = "app"
    mode = "External"
    preset = "dialogovo_alice_apps_sticky"
    app_id = "d82cfec1-bb01-4c7a-8672-b836dd72ff61"
    description = "Поиск ближайших заправок, цен на топливо и маршрутов"
    timeout = 60.0

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        nsb = card.get("neuro_structured_blocks_card")
        if nsb and "plain_text" in nsb:
            pt = nsb["plain_text"].strip()
            if pt and pt not in text_blocks:
                text_blocks.append(pt)
            return True

        tc = card.get("text_card")
        if tc and "text" in tc:
            t = tc["text"].strip()
            if t and t not in text_blocks:
                text_blocks.append(t)
            return True

        return False