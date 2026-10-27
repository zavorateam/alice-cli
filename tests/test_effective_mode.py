import json
from pathlib import Path
import pytest

FIX = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_effective_mode_module_exists():
    """Модуль tools.effective должен существовать и экспортировать normalize_effective_mode."""
    from tools.effective import normalize_effective_mode, EffectiveMode  # noqa: F401


def test_real_nested_only():
    from tools.effective import normalize_effective_mode
    raw = _load("vins_real_nested_only.json")
    eff = normalize_effective_mode(raw["payload"], requested_mode="Pro")
    assert eff.effective_mode == "Base"
    assert eff.source_path == "payload.response.alice_2_effective_settings"


def test_real_both_forms():
    from tools.effective import normalize_effective_mode
    raw = _load("vins_real_both_forms.json")
    eff = normalize_effective_mode(raw["payload"], requested_mode="Pro")
    assert eff.effective_mode == "Base"
    assert eff.source_path == "payload.effective_alice_2_settings"


def test_legacy_with_trial():
    from tools.effective import normalize_effective_mode
    raw = _load("vins_legacy_effective.json")
    eff = normalize_effective_mode(raw["payload"], requested_mode="External")
    assert eff.effective_mode == "External"
    assert eff.trial_left == 3
    assert eff.trial_limit == 5
    assert eff.source_path == "payload.alice_2_effective_settings"


def test_no_effective():
    from tools.effective import normalize_effective_mode
    raw = _load("vins_no_effective.json")
    eff = normalize_effective_mode(raw["payload"], requested_mode="Pro")
    assert eff.effective_mode is None
    assert eff.source_path is None


def test_pro_to_base_is_degraded():
    from tools.effective import normalize_effective_mode
    raw = _load("vins_real_both_forms.json")
    eff = normalize_effective_mode(raw["payload"], requested_mode="Pro")
    assert eff.degraded is True


def test_external_to_base_is_degraded():
    from tools.effective import normalize_effective_mode
    payload = {"response": {"alice_2_effective_settings": {"effective_settings": {"mode": "Base"}}}}
    eff = normalize_effective_mode(payload, requested_mode="External")
    assert eff.effective_mode == "Base"
    assert eff.degraded is True


def test_current_server_bug_top_level_never_read():
    """Регрессия: старый код смотрел payload['alice_2_effective_settings'], которого в A5 нет.
    Проверяем, что normalize находит и без этого ключа (nested-only)."""
    from tools.effective import normalize_effective_mode
    raw = _load("vins_real_nested_only.json")
    assert "alice_2_effective_settings" not in raw["payload"]   # доказываем, что ключа нет
    eff = normalize_effective_mode(raw["payload"], requested_mode="Pro")
    assert eff.effective_mode == "Base"
