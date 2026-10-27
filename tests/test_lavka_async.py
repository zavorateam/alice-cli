"""§8: lavka.parse_card не блокирует event loop."""
import asyncio
import inspect


def test_lavka_parse_card_is_async():
    from agents.lavka import LavkaAgent
    assert inspect.iscoroutinefunction(LavkaAgent.parse_card), (
        "LavkaAgent.parse_card должен быть async — иначе синхронный urlopen "
        "блокирует event loop до 5 сек"
    )


def test_lavka_no_sync_urllib():
    from pathlib import Path
    src = Path("agents/lavka.py").read_text()
    assert "urllib.request.urlopen" not in src, (
        "Синхронный urlopen не должен присутствовать в lavka.py"
    )


def test_lavka_async_callable():
    from agents.lavka import LavkaAgent
    agent = LavkaAgent()
    # вызов без данных не должен падать
    from agents.base import AliceTurnResponse
    turn = AliceTurnResponse(dialog_id="d", request_id="r")
    result = asyncio.run(agent.parse_card({}, turn, []))
    assert result is False
