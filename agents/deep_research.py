import json
from typing import Any, Dict, List
from agents.base import AgentWidget, AliceAgent, AliceTurnResponse


class DeepResearchAgent(AliceAgent):
    id = "deep_research"
    name = "Исследовать (Deep Research)"
    aliases = ["исследовать", "deep_research", "исследование", "research"]
    type = "agent"
    mode = "External"
    preset = "agents_deep_research"
    app_id = None
    description = "Глубокий автономный веб-поиск и синтез информации из первоисточников"
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

        if ui_name == "deepresearch_agent_card":
            prog = data.get("progress", {})
            for step in prog.get("steps", []):
                s_title = step.get("title", "")
                s_stat = step.get("status", "")
                label = f"{s_title} [{s_stat}]"
                if label not in turn.thinking_logs:
                    turn.thinking_logs.append(label)

            sources = data.get("sources", [])
            if sources:
                turn.widgets.append(AgentWidget(
                    component_name="deep_research_sources",
                    title="Источники исследования",
                    raw_data={"sources": sources[:10]},
                ))
            return True

        return False