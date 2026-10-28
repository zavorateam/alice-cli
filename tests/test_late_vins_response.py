"""Live-bug 2026-10-28: поздний VinsResponse не должен создавать effective_by_req.

Воспроизведение: SSE запрос завершился (finally очистил pending_responses),
через 4 сек приходит VinsResponse с тем же ref_id. Раньше:
  - effective_by_req[ref_id] создавался заново
  - finally уже прошёл → запись утекла навсегда
Теперь: _handle_vins_response видит ref_id not in _pending_responses и skip.
"""
import asyncio

from alice_auth import AliceCredentials
from tools.transport import AliceWsEngine


def _engine():
    return AliceWsEngine(AliceCredentials(auth_type="token", token="y0_test"))


def test_late_vins_response_does_not_create_effective_state():
    eng = _engine()
    # pending_responses пуст — запрос уже завершён
    header = {"refMessageId": "01a18172-aaaa-bbbb-cccc-123456789abc"}
    payload = {"effective_alice_2_settings": {"mode": "Base"}}
    eng._handle_vins_response(header, payload)
    assert eng.effective_by_req == {}, (
        f"утечка effective_by_req для мёртвого ref_id: {eng.effective_by_req}"
    )
    # trial_state тоже не должен обновиться для мёртвого запроса
    assert eng.expert_trial_left is None


def test_active_vins_response_still_processed():
    eng = _engine()
    ref_id = "01a18172-aaaa-bbbb-cccc-123456789abc"
    eng._pending_responses[ref_id] = asyncio.Queue()
    eng.requested_mode_by_req[ref_id] = "Pro"
    header = {"refMessageId": ref_id}
    payload = {"effective_alice_2_settings": {"mode": "Base"}}
    eng._handle_vins_response(header, payload)
    assert ref_id in eng.effective_by_req
    eff = eng.effective_by_req[ref_id]
    assert eff.effective_mode == "Base"
    assert eff.requested_mode == "Pro"
    assert eff.degraded is True


def test_late_vins_response_does_not_touch_expert_trial():
    """Поздний trial_state не должен менять глобальный expert_trial_left."""
    eng = _engine()
    eng.expert_trial_left = 5
    eng.expert_trial_limit = 10
    header = {"refMessageId": "dead-ref-0000"}
    payload = {
        "effective_alice_2_settings": {
            "mode": "External",
            "trial_state": {"left_count": 0, "limit": 5},
        }
    }
    eng._handle_vins_response(header, payload)
    assert eng.expert_trial_left == 5
    assert eng.expert_trial_limit == 10


def test_log_uses_ref_suffix(caplog):
    """Лог должен идентифицировать запрос по последним 8 hex (uuid7 suffix)."""
    import logging
    eng = _engine()
    ref_id = "01a18172-aaaa-bbbb-cccc-123456789abc"
    eng._pending_responses[ref_id] = asyncio.Queue()
    eng.requested_mode_by_req[ref_id] = "Base"
    header = {"refMessageId": ref_id}
    payload = {"effective_alice_2_settings": {"mode": "Base"}}
    with caplog.at_level(logging.INFO):
        eng._handle_vins_response(header, payload)
    text = "\n".join(r.message for r in caplog.records)
    # последние 8 hex: "456789ab" → "56789abc" если 8 → "456789abc"? проверяем конец
    assert "ref=3456789abc" not in text and ref_id[-8:] in text, (
        f"лог не содержит suffix ref: {text!r}"
    )
