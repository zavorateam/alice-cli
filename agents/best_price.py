import json
from typing import Any, Dict, List
from agents.base import AgentWidget, AliceAgent, AliceTurnResponse


class BestPriceAgent(AliceAgent):
    id = "best_price"
    name = "Найти дешевле"
    aliases = ["дешевле", "best_price", "скидки", "цены", "bestprice"]
    type = "agent"
    mode = "External"
    preset = "agents_best_price"
    app_id = None
    description = "Автономный поиск лучших цен и предложений по интернет-магазинам"
    timeout = 180.0

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

        # 1. Регион поиска
        if ui_name == "EAliceGeoChange":
            geo_t = data.get("text", "").strip()
            if geo_t and geo_t not in text_blocks:
                text_blocks.append(geo_t)
            return True

        # 2. Исходная карточка товара или найденное лучшее предложение
        elif ui_name in ("EAliceOfferCard", "EAliceOffer"):
            prod_name = data.get("productName") or data.get("title") or "Товар"
            if isinstance(prod_name, dict):
                prod_name = prod_name.get("text", "")

            price_obj = data.get("price", {})
            price_val = price_obj.get("value") if isinstance(price_obj, dict) else data.get("price", "")
            curr = price_obj.get("currency") if isinstance(price_obj, dict) else "₽"

            store = (
                data.get("shopName")
                or data.get("greenUrl", {}).get("text")
                or data.get("store")
                or "Магазин"
            )
            offer_url = data.get("url") or data.get("directUrl") or ""
            disc = data.get("discount", {})
            disc_val = disc.get("value") or disc.get("percent") or ""

            header_icon = "🎉 **Найдено дешевле!**" if ui_name == "EAliceOffer" else "🏷️ **Карточка товара**"
            offer_text = f"{header_icon}\n**{prod_name}**\n• Магазин: {store}\n• Цена: {price_val} {curr}"
            if disc_val:
                offer_text += f" (Выгода: {disc_val} ₽)"
            if offer_url:
                offer_text += f"\n• Ссылка: {offer_url}"

            if offer_text not in text_blocks:
                text_blocks.append(offer_text)

            if offer_url and not any(w.url == offer_url for w in turn.widgets):
                turn.widgets.append(AgentWidget(
                    component_name=ui_name,
                    title=f"Купить в {store} ({price_val} {curr})",
                    url=offer_url,
                    raw_data=data,
                ))
            return True

        # 3. Галерея альтернативных предложений магазинов
        elif ui_name == "EAliceAdvGallery":
            items = data.get("items") or data.get("offers") or data.get("snippets") or []
            for item in items:
                i_name = item.get("title") or item.get("productName") or "Предложение"
                if isinstance(i_name, dict):
                    i_name = i_name.get("text", "")
                i_price = item.get("price", {})
                i_pval = i_price.get("value") if isinstance(i_price, dict) else item.get("price", "")
                i_curr = i_price.get("currency") if isinstance(i_price, dict) else "₽"
                i_store = item.get("shopName") or item.get("greenUrl", {}).get("text") or "Магазин"
                i_url = item.get("url") or item.get("directUrl") or ""

                if i_url and not any(w.url == i_url for w in turn.widgets):
                    turn.widgets.append(AgentWidget(
                        component_name=ui_name,
                        title=f"{i_store}: {i_name[:40]} — {i_pval} {i_curr}",
                        url=i_url,
                        raw_data=item,
                    ))
            return True

        return False