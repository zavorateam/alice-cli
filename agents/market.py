import json
import urllib.parse
from typing import Any, Dict, List
from agents.base import AgentWidget, AliceAgent, AliceTurnResponse


class MarketAgent(AliceAgent):
    id = "market"
    name = "Яндекс Маркет"
    aliases = ["маркет", "market", "ямаркет", "яндекс.маркет"]
    type = "app"
    mode = "External"
    preset = "dialogovo_alice_apps_sticky"
    app_id = "9562a4ff-bafd-4949-92fa-b9b2206fae4c"
    description = "Поиск товаров, скидок и сравнение цен на Яндекс Маркете"
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

        if ui_name == "AliceAppsWidget":
            title = data.get("sharedInfo", {}).get("app", {}).get("title") or "Маркет"
            meta_tpl = data.get("meta", {}).get("outputTemplate", "")
            direct_url = meta_tpl
            if "market.yandex.ru" in meta_tpl and "product=" in meta_tpl:
                try:
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(meta_tpl).query)
                    pid = qs.get("product", [""])[0].split(",")[0].strip()
                    if pid:
                        direct_url = f"https://market.yandex.ru/product/{pid}"
                except Exception:
                    pass

            # Исключаем добавление дубликата ссылки
            if not any(w.url == direct_url for w in turn.widgets):
                turn.widgets.append(AgentWidget(
                    component_name=ui_name,
                    app_id=self.app_id,
                    title=title,
                    url=direct_url,
                    raw_data=data,
                ))
            return True

        return False