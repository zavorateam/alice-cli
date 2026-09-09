import json
from typing import Any, Dict, List
from agents.base import AgentWidget, AliceAgent, AliceTurnResponse, SuggestionAction, decode_dialog_action


class TaxiAgent(AliceAgent):
    id = "taxi"
    name = "Яндекс Такси"
    aliases = ["такси", "taxi", "go"]
    type = "app"
    mode = "External"
    preset = "dialogovo_alice_apps_sticky"
    app_id = "b8c1cc34-63d9-439c-96ac-b29d0c00dd49"
    description = "Заказ такси, расчет времени и подбор оптимальных тарифов"
    timeout = 60.0

    def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
        rich = card.get("rich_uicard", {})
        ui_name = rich.get("uicomponent_name")
        raw_jd = rich.get("data", {}).get("json_data")

        if not ui_name or not raw_jd:
            return False

        try:
            data = json.loads(raw_jd)
        except Exception:
            return False

        # Шаги рассуждений
        if ui_name == "AliceAgentAppThinking":
            tpl = data.get("template")
            if tpl and tpl not in turn.thinking_logs:
                turn.thinking_logs.append(tpl)
            return True

        # Заголовки и статус поездки
        elif ui_name == "go_markdown":
            t = data.get("text", "").strip()
            if t and t not in text_blocks:
                text_blocks.append(t)
            return True

        # Список сохраненных адресов
        elif ui_name == "go_suggest_list":
            for s in data.get("suggests", []):
                t_obj = s.get("title", {})
                title = t_obj.get("text", "") if isinstance(t_obj, dict) else str(t_obj)
                sub_obj = s.get("subtitle", {})
                sub = sub_obj.get("text", "") if isinstance(sub_obj, dict) else ""
                full_addr = f"{sub}, {title}" if (sub and sub not in title) else title

                act = s.get("action", {})
                deeplink = act.get("deeplink", "") if isinstance(act, dict) else str(act)
                payload = decode_dialog_action(deeplink) or full_addr

                if full_addr and not any(x.title == full_addr for x in turn.suggestions):
                    turn.suggestions.append(SuggestionAction(title=full_addr, payload=payload))
            return True

        # Карточки с тарифами (Эконом, Комфорт и т.д.)
        elif ui_name == "taxi_offers":
            for snip in data.get("snippets", []):
                hdr = snip.get("header", {})
                tariff_name = "".join(i.get("text", "") for i in hdr.get("title", {}).get("items", []))
                eta = "".join(i.get("text", "") for i in hdr.get("subtitle", {}).get("items", []))
                duration = "".join(i.get("text", "") for i in hdr.get("center_text", {}).get("items", []))
                price = "".join(i.get("text", "") for i in hdr.get("end_text", {}).get("items", []))

                act = snip.get("action", {})
                deeplink = act.get("deeplink", "") if isinstance(act, dict) else str(act)
                payload = decode_dialog_action(deeplink) or tariff_name

                display_title = f"{tariff_name} — {price} (~{eta} подача, ~{duration} в пути)"
                if not any(x.payload == payload for x in turn.suggestions):
                    turn.suggestions.append(SuggestionAction(title=display_title, payload=payload))
            return True

        # Виджет кнопки оформления заказа
        elif ui_name == "AliceAppsWidget":
            tool_output = data.get("initialToolOutput", {}).get("structuredContent", {})
            meta_tpl = data.get("meta", {}).get("outputTemplate", "")
            turn.widgets.append(AgentWidget(
                component_name=ui_name,
                app_id=self.app_id,
                title="Такси (Заказ)",
                url=meta_tpl,
                raw_data=tool_output,
            ))
            return True

        return False