"""Конкурентные сессии через ОДИН общий manager.

Production: alice_server.main_async --http создаёт SessionRegistry с
manager_factory=lambda: manager — то есть все сессии используют один
AliceAgentManager с общим WS-соединением. Тест раньше создавал
отдельный FakeManager на каждую сессию и не воспроизводил гонки
`active_dialog_id`.

Здесь:
  - один `_SharedFakeMgr` на всё;
  - история хранится в history_by_dialog[dialog_id];
  - явно проверяем, что два X-Alice-Session-ID получают РАЗНЫЕ dialog_id
    и их история в manager-е не путается;
  - фиксируем известное ограничение: manager.active_dialog_id глобальный,
    поэтому для HTTP-сессий нельзя полагаться на него — только на явный
    dialog_id.
"""
import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes
from tools.manager import SessionRegistry


class _SharedFakeMgr:
    """Один manager на все сессии — как в production.

    Активная сессия меняется через create_new_dialog/send_message, как в
    реальном AliceAgentManager. История разделена по dialog_id.
    """

    def __init__(self):
        self.active_dialog_id = "dialog-initial"
        self.active_agent_id = "pro"
        self.history_by_dialog = {}
        self._dialog_seq = 0
        self._create_lock = asyncio.Lock()
        self.create_calls = 0

    async def create_new_dialog(self, agent_id="pro", is_ephemeral=False):
        async with self._create_lock:
            self._dialog_seq += 1
            self.create_calls += 1
            new_id = f"dialog-{self._dialog_seq}"
            self.active_dialog_id = new_id
            self.active_agent_id = agent_id
            self.history_by_dialog.setdefault(new_id, [])
            return new_id

    async def send_message(self, text, agent_id="pro", dialog_id=None, is_suggest=False):
        from agents.base import AliceTurnResponse
        target = dialog_id or self.active_dialog_id
        self.active_dialog_id = target
        self.active_agent_id = agent_id
        self.history_by_dialog.setdefault(target, []).append(text)
        yield AliceTurnResponse(
            dialog_id=target,
            request_id=f"r-{target}-{len(self.history_by_dialog[target])}",
            text=f"{target}:{len(self.history_by_dialog[target])}:{text}",
            is_finished=True,
        )

    async def rpc_delete_dialog(self, *a, **kw):
        return None

    @property
    def rpc(self):
        class _R:
            async def delete_dialog(self, *a, **kw):
                return None
        return _R()


def _app(registry):
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, registry)
    return TestClient(app)


def _shared_registry():
    mgr = _SharedFakeMgr()

    async def factory():
        return mgr

    return mgr, SessionRegistry(manager_factory=factory)


def _post(client, sid, text, model="alice-base"):
    headers = {"X-Alice-Session-ID": sid} if sid else {}
    return client.post("/v1/chat/completions",
                       headers=headers,
                       json={"model": model,
                             "messages": [{"role": "user", "content": text}]})


# ---------------- изоляция истории ----------------


def test_two_sessions_share_one_manager_but_isolated_history():
    """Один manager, две сессии → два dialog_id, история не путается."""
    mgr, reg = _shared_registry()
    client = _app(reg)

    r1 = _post(client, "S1", "first")
    r2 = _post(client, "S2", "second")
    r1b = _post(client, "S1", "third")

    assert r1.status_code == 200, r1.text[:300]
    assert r2.status_code == 200, r2.text[:300]
    assert r1b.status_code == 200, r1b.text[:300]

    # 2 dialog_id (по числу уникальных сессий), 3 сообщения
    assert mgr.create_calls == 2
    assert len(mgr.history_by_dialog) == 2

    # Первый dialog (S1) содержит first+third; второй (S2) — second
    d1, d2 = sorted(mgr.history_by_dialog.keys(), key=lambda k: int(k.split("-")[1]))
    assert mgr.history_by_dialog[d1] == ["first", "third"]
    assert mgr.history_by_dialog[d2] == ["second"]

    # И сам ответ подтверждает, что dialog_id в response — разные
    body1 = r1.json()
    body2 = r2.json()
    body1b = r1b.json()
    # Мы не парсим dialog_id из response (в OpenAI-формате его нет) —
    # проверяем по содержимому, что session S1 не попала в d2
    assert "first" in mgr.history_by_dialog[d1]
    assert "first" not in mgr.history_by_dialog[d2]


def test_active_dialog_id_drifts_but_history_per_dialog_intact():
    """Известное ограничение: manager.active_dialog_id глобальный,
    но история в history_by_dialog[dialog_id] не путается.

    Это документирует, что HTTP-сессия должна опираться на свой dialog_id,
    а не на manager.active_dialog_id.
    """
    mgr, reg = _shared_registry()
    client = _app(reg)

    _post(client, "S1", "one")
    _post(client, "S2", "two")
    # После S2 active_dialog_id = dialog S2 — глобальный дрейф
    assert mgr.active_dialog_id == "dialog-2"
    # Но история S1 осталась в dialog-1
    assert mgr.history_by_dialog["dialog-1"] == ["one"]
    assert mgr.history_by_dialog["dialog-2"] == ["two"]


# ---------------- без заголовка ----------------


def test_no_header_creates_isolated_session():
    """Без X-Alice-Session-ID каждый запрос = новая сессия.

    Проверяем не только что сессий 3, но и что после каждого запроса
    создаётся ровно один новый dialog_id и одна запись в history_by_dialog.
    """
    mgr, reg = _shared_registry()
    client = _app(reg)
    for _ in range(3):
        r = _post(client, None, "x")
        assert r.status_code == 200, r.text[:200]
    assert mgr.create_calls == 3
    assert len(mgr.history_by_dialog) == 3
    for did, history in mgr.history_by_dialog.items():
        assert history == ["x"], f"{did}: {history}"


# ---------------- TTL и MAX_SESSIONS ----------------


def test_session_registry_ttl_evicts_idle():
    """Сессии старше TTL удаляются при следующем acquire."""
    mgr, reg = _shared_registry()
    reg._ttl = 0.01  # 10 мс
    client = _app(reg)

    r = _post(client, "S1", "first")
    assert r.status_code == 200
    assert len(reg) == 1

    # Ждём, пока сессия «устареет»
    time.sleep(0.05)

    # Следующий acquire с другим sid — должен выкинуть S1
    r2 = _post(client, "S2", "second")
    assert r2.status_code == 200
    assert len(reg) == 1, f"TTL не сработал: {len(reg)} сессий"
    assert "S1" not in reg._sessions
    assert "S2" in reg._sessions


def test_session_registry_max_sessions_evicts_oldest():
    mgr, reg = _shared_registry()
    reg._max = 2
    client = _app(reg)
    for sid in ("A", "B", "C"):
        r = _post(client, sid, f"msg-{sid}")
        assert r.status_code == 200
    assert len(reg) == 2, f"MAX_SESSIONS не работает: {len(reg)}"
    assert "A" not in reg._sessions  # A — самый старый, выкинут
    assert {"B", "C"}.issubset(reg._sessions)


def test_session_registry_max_sessions_exact_boundary():
    """При _max=2: A, B — ок; C — выкидывает A; D — выкидывает B; ..."""
    mgr, reg = _shared_registry()
    reg._max = 2
    client = _app(reg)
    for sid in ("A", "B", "C", "D"):
        r = _post(client, sid, f"msg-{sid}")
        assert r.status_code == 200
    assert len(reg) == 2
    assert reg._sessions.keys() == {"C", "D"}, f"got {set(reg._sessions)}"


def test_session_registry_max_zero_means_unlimited():
    mgr, reg = _shared_registry()
    reg._max = 0   # 0 = без лимита
    client = _app(reg)
    for sid in ("A", "B", "C", "D", "E"):
        _post(client, sid, "x")
    assert len(reg) == 5


def test_session_registry_reuse_does_not_evict():
    """Повторный acquire того же sid не должен ничего выкидывать."""
    mgr, reg = _shared_registry()
    reg._max = 2
    client = _app(reg)
    _post(client, "A", "1")
    _post(client, "B", "2")
    _post(client, "A", "3")   # reuse A, не новый
    _post(client, "A", "4")
    assert len(reg) == 2
    assert reg._sessions.keys() == {"A", "B"}


def test_session_registry_last_used_updated_on_each_call():
    mgr, reg = _shared_registry()
    client = _app(reg)
    _post(client, "S1", "one")
    _post(client, "S1", "two")
    # last_used для S1 обновился, сессий по-прежнему 1
    assert len(reg) == 1
    assert "S1" in reg._last_used
