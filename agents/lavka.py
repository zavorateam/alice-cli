import json
import urllib.request
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

            if p_ids:
                try:
                    # Пробуем получить deepLinks из API Лавки
                    req_payload = {"productIds": p_ids}
                    if "location" in tool_output:
                        req_payload["position"] = {"location": tool_output["location"]}
                    elif "position" in tool_output:
                        req_payload["position"] = tool_output["position"]

                    req = urllib.request.Request(
                        "https://lavka.yandex.ru/api/v1/providers/v1/products/list/by-id",
                        data=json.dumps(req_payload).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0",
                            "x-lavka-web-locale": "ru-RU",
                            "x-lavka-web-city": "213",
                            "x-alice-agent": "true"
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=5.0) as response:
                        resp_data = json.loads(response.read().decode("utf-8"))

                    products = resp_data.get("products", [])
                    if products:
                        for p in products:
                            dl = p.get("deepLink")
                            title = p.get("title")
                            if dl and title:
                                # Формируем ссылку на карточку товара
                                url = f"https://lavka.yandex.ru/213/good/{dl}"
                                turn.widgets.append(AgentWidget(
                                    component_name=ui_name,
                                    app_id=self.app_id,
                                    title=title,
                                    url=url,
                                    raw_data=p,
                                ))

                        # Если распарсили хотя бы один товар, не добавляем общую ссылку
                        if turn.widgets:
                            return True
                except Exception:
                    # Если запрос не удался (timeout/500/403), переходим к фолбэку ниже
                    pass

            # Фолбэк: если товаров нет или API недоступно, возвращаем ссылку на общую корзину
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
