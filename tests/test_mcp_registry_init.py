"""AliceMcpServer должен сам обеспечивать registry, не зависеть от agents/__init__.py."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_import_has_no_side_effect_on_registry():
    """Импорт agents пакета НЕ должен заполнять registry (subprocess, чистое окружение)."""
    script = (
        "import sys; sys.path.insert(0, %r); "
        "from agents.registry import registry; "
        "print(len(registry.list_all()))"
    ) % str(ROOT)
    env = os.environ.copy()
    for k in ("ALICE_TOKEN", "YANDEX_TOKEN", "ALICE_COOKIES"):
        env.pop(k, None)
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0", (
        f"импорт agents заполнил registry ({out.stdout.strip()} агентов). "
        "auto_discover должен вызываться явно."
    )


def test_ensure_agents_registered_idempotent():
    from agents import ensure_agents_registered
    from agents.registry import registry

    n1 = ensure_agents_registered()
    n2 = ensure_agents_registered()
    assert n1 == n2
    assert n1 >= 3
    ids = {a.id for a in registry.list_all()}
    assert {"pro", "base", "expert"}.issubset(ids)


def test_mcp_server_ensures_agents():
    """AliceMcpServer.__init__ сам вызывает ensure_agents_registered().

    Восстанавливает registry в finally, чтобы не ломать другие тесты.
    """
    from agents.registry import registry
    from agents import reset_registration_state, ensure_agents_registered
    from alice_server import AliceMcpServer

    ensure_agents_registered()  # гарантированно заполним
    saved_agents = dict(registry._agents)
    saved_aliases = dict(registry._alias_map)
    try:
        reset_registration_state()
        registry._agents.clear()
        registry._alias_map.clear()
        assert len(registry.list_all()) == 0
        AliceMcpServer(manager=None)
        ids = {a.id for a in registry.list_all()}
        assert {"pro", "base", "expert"}.issubset(ids)
    finally:
        registry._agents.clear()
        registry._agents.update(saved_agents)
        registry._alias_map.clear()
        registry._alias_map.update(saved_aliases)
        # помечаем _discovered=True, чтобы не пересканировать зря
        import agents as _ag
        _ag._discovered = True


def test_tools_definition_nonempty_without_creds():
    from alice_server import AliceMcpServer
    mcp = AliceMcpServer(manager=None)
    tools = mcp._get_tools_definition()
    names = {t["name"] for t in tools}
    assert "alice_ask_pro" in names
    assert "alice_ask_expert" in names
    assert "alice_click_action" in names
