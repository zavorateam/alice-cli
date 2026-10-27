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

    async def parse_card(self, card: Dict[str, Any], turn: AliceTurnResponse, text_blocks: List[str]) -> bool:
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
                products = await self._fetch_products(p_ids, tool_output)
                if products:
                    for prod in products:
                        dl = prod.get("deepLink")
                        title = prod.get("title")
                        if dl and title:
                            url = f"https://lavka.yandex.ru/213/good/{dl}"
                            turn.widgets.append(AgentWidget(
                                component_name=ui_name,
                                app_id=self.app_id,
                                title=title,
                                url=url,
                                raw_data=prod,
                            ))
                    if turn.widgets:
                        return True

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

    async def _fetch_products(self, p_ids, tool_output):
        """Асинхронный запрос deepLinks через aiohttp, timeout 5s. Возвращает список или None."""
        try:
            import aiohttp
        except ImportError:
            return None
        req_payload = {"productIds": p_ids}
        if "location" in tool_output:
            req_payload["position"] = {"location": tool_output["location"]}
        elif "position" in tool_output:
            req_payload["position"] = tool_output["position"]
        try:
            timeout = aiohttp.ClientTimeout(total=5.0, connect=3.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    "https://lavka.yandex.ru/api/v1/providers/v1/products/list/by-id",
                    json=req_payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0",
                        "x-lavka-web-locale": "ru-RU",
                        "x-lavka-web-city": "213",
                        "x-alice-agent": "true",
                    },
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
            return data.get("products", []) or None
        except Exception:
            return None
