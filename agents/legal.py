import json
from typing import Any, Dict, List
from agents.base import AliceAgent, AliceTurnResponse


class LegalAgent(AliceAgent):
    id = "legal"
    name = "Нейроюрист"
    aliases = ["юрист", "legal", "право", "закон", "консультация"]
    type = "app"
    mode = "External"
    preset = "dialogovo_alice_apps_sticky"
    app_id = "16d44243-2e13-4aee-b6b6-5aabc806eb6d"
    description = "Юридические консультации со ссылками на статьи законов РФ"
    timeout = 90.0

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        nsb = card.get("neuro_structured_blocks_card")
        if nsb and "plain_text" in nsb:
            pt = nsb["plain_text"].strip()
            if pt and pt not in text_blocks:
                text_blocks.append(pt)
            return True

        rich = card.get("rich_uicard", {})
        ui_name = rich.get("uicomponent_name")
        raw_jd = rich.get("data", {}).get("json_data")

        if not ui_name or not raw_jd:
            return False

        try:
            data = json.loads(raw_jd)
        except Exception:
            return False

        if ui_name == "processing_steps":
            steps = data.get("progress", {}).get("steps", [])
            for st in steps:
                st_t = st.get("title", "")
                st_s = st.get("status", "")
                label = f"{st_t} [{st_s}]"
                if label not in turn.thinking_logs:
                    turn.thinking_logs.append(label)
            return True

        return False