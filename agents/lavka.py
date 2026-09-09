import json
from typing import Any, Dict, List
from agents.base import AgentWidget, AliceAgent, AliceTurnResponse, SuggestionAction, decode_dialog_action


class LavkaAgent(AliceAgent):
    id = "lavka"
    name = "Яндекс Лавка"
    aliases = ["лавка", "lavka", "продукты", "магазин"]
    type = "app"
    mode = "External"
    preset = "dialogovo_alice_apps_sticky"
    app_id = "70f345ca-f81d-4aeb-8cb2-1c68693c5dc7"
    description = "Экспресс-доставка продуктов, готовой еды и сбор корзины"
    timeout = 60.0

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        tc = card.get("text_card")
        if tc and "text" in tc:
            t = tc["text"].strip()
            if t and t not in text_blocks:
                text_blocks.append(t)
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

        if ui_name == "AliceAgentAppThinking":
            tpl = data.get("template")
            if tpl and tpl not in turn.thinking_logs:
                turn.thinking_logs.append(tpl)
            return True

        elif ui_name == "AliceAppsWidget":
            tool_output = data.get("initialToolOutput", {}).get("structuredContent", {})
            meta_tpl = data.get("meta", {}).get("outputTemplate", "")
            p_ids = tool_output.get("productIds", [])
            w_title = f"Посмотрела в сервисе Лавка ({len(p_ids)} товаров)" if p_ids else "Посмотрела в сервисе Лавка"
            turn.widgets.append(AgentWidget(
                component_name=ui_name,
                app_id=self.app_id,
                title=w_title,
                url=meta_tpl,
                raw_data=tool_output,
            ))
            return True

        elif ui_name == "go_bubbles":
            for b in data.get("timeslots", {}).get("buttons", []):
                title = b.get("title", "").strip()
                act = b.get("action", {})
                deeplink = act.get("deeplink", "") if isinstance(act, dict) else str(act)
                payload = decode_dialog_action(deeplink) or title
                if title and not any(x.title == title for x in turn.suggestions):
                    turn.suggestions.append(SuggestionAction(title=title, payload=payload))
            return True

        return False