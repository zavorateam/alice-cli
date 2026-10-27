"""§9: политика re-register + идемпотентность."""
import pytest


class _A:
    def __init__(self, id, name, aliases):
        from agents.base import AliceAgent
        self.id = id
        self.name = name
        self.aliases = list(aliases)
        self.type = "model"
        self.mode = "Pro"
        self.preset = ""
        self.app_id = None
        self.description = ""
        self.timeout = 60.0


def _make_agent(id, name, aliases):
    from agents.base import AliceAgent
    class _Concrete(AliceAgent):
        pass
    a = _Concrete()
    a.id = id
    a.name = name
    a.aliases = list(aliases)
    a.type = "model"
    a.mode = "Pro"
    a.preset = ""
    a.app_id = None
    a.description = ""
    a.timeout = 60.0
    return a


def test_reregister_removes_stale_aliases():
    from agents.registry import AgentRegistry
    reg = AgentRegistry()
    a1 = _make_agent("x", "Alpha", ["alpha", "alias_old"])
    reg.register(a1)
    assert reg.get("alias_old") is a1

    a2 = _make_agent("x", "Alpha", ["alpha", "alias_new"])
    reg.register(a2)
    # alias_old указывал на старый объект — должен быть недоступен
    # (fallback в get() вернёт "pro" если есть, или any)
    # проверим что _alias_map не содержит stale
    assert "alias_old" not in reg._alias_map or reg._alias_map["alias_old"] is a2
    assert reg.get("alias_new") is a2
    assert reg.get("x") is a2


def test_ensure_agents_registered_retry_on_zero(monkeypatch):
    """_discovered не должен оставаться True при нулевом обнаружении."""
    import agents
    from agents import registry as _r
    # обнулим
    agents._discovered = False
    calls = {"n": 0}
    def fake_discover(*a, **kw):
        calls["n"] += 1
        return 0  # эмулируем неудачу
    monkeypatch.setattr(_r, "auto_discover", fake_discover)
    agents.ensure_agents_registered()
    assert calls["n"] == 1
    assert agents._discovered is False, "0 агентов не должен фиксировать _discovered"
    # повторный вызов должен попробовать снова
    agents.ensure_agents_registered()
    assert calls["n"] == 2


def test_ensure_agents_registered_sets_flag_on_success(monkeypatch):
    import agents
    from agents import registry as _r
    agents._discovered = False
    monkeypatch.setattr(_r, "auto_discover", lambda *a, **kw: 3)
    agents.ensure_agents_registered()
    assert agents._discovered is True
