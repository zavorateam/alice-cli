import json
from pathlib import Path

from alice_server import AliceWsEngine
from alice_auth import AliceCredentials

FIX = Path(__file__).resolve().parent / "fixtures"


def _engine():
    creds = AliceCredentials(auth_type="token", token="y0_test")
    return AliceWsEngine(creds)


def test_handle_vins_response_nested_only():
    import asyncio
    eng = _engine()
    raw = json.loads((FIX / "vins_real_nested_only.json").read_text())
    header = raw["header"]
    payload = raw["payload"]
    # Регистрируем запрос как активный: иначе _handle_vins_response
    # пропустит late-кадр (см. tests/test_late_vins_response.py).
    eng._pending_responses[header["refMessageId"]] = asyncio.Queue()
    eng.requested_mode_by_req[header["refMessageId"]] = "Pro"
    eng._handle_vins_response(header, payload)
    eff = eng.effective_by_req[header["refMessageId"]]
    assert eff.effective_mode == "Base"
    assert eff.degraded is True
    assert eff.source_path == "payload.response.alice_2_effective_settings"


def test_handle_vins_response_top_and_nested():
    import asyncio
    eng = _engine()
    raw = json.loads((FIX / "vins_real_both_forms.json").read_text())
    header = raw["header"]
    eng._pending_responses[header["refMessageId"]] = asyncio.Queue()
    eng.requested_mode_by_req[header["refMessageId"]] = "Pro"
    eng._handle_vins_response(header, raw["payload"])
    eff = eng.effective_by_req[header["refMessageId"]]
    assert eff.effective_mode == "Base"
    assert eff.degraded is True


def test_handle_vins_response_trial_state():
    import asyncio
    eng = _engine()
    raw = json.loads((FIX / "vins_legacy_effective.json").read_text())
    header = raw["header"]
    eng._pending_responses[header["refMessageId"]] = asyncio.Queue()
    eng.requested_mode_by_req[header["refMessageId"]] = "External"
    eng._handle_vins_response(header, raw["payload"])
    assert eng.expert_trial_left == 3
    assert eng.expert_trial_limit == 5


def test_handle_vins_response_missing_ref_does_nothing():
    eng = _engine()
    before = dict(eng.effective_by_req)
    eng._handle_vins_response({}, {"effective_alice_2_settings": {"mode": "Base"}})
    assert eng.effective_by_req == before


def test_handle_vins_response_no_effective_field():
    import asyncio
    eng = _engine()
    raw = json.loads((FIX / "vins_no_effective.json").read_text())
    header = raw["header"]
    eng._pending_responses[header["refMessageId"]] = asyncio.Queue()
    eng.requested_mode_by_req[header["refMessageId"]] = "Pro"
    eng._handle_vins_response(header, raw["payload"])
    eff = eng.effective_by_req[header["refMessageId"]]
    assert eff.effective_mode is None
    assert eff.degraded is False
