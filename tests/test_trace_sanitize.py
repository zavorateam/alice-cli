"""Санитизация трасс: UUID устройства не должен попадать в events.jsonl.

A5 payload содержал uuid в event.payload.application.uuid — этот ключ
не покрывался _SECRET_KEY (были device_uuid/device_id, но не uuid).
"""
import importlib.util
import json
import os

import pytest


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """Загружает SITE_CUSTOMIZE (из collect_alice_traces) как модуль."""
    monkeypatch.setenv("ALICE_TRACE_DIR", str(tmp_path))
    monkeypatch.delenv("ALICE_TOKEN", raising=False)
    monkeypatch.delenv("YANDEX_TOKEN", raising=False)
    monkeypatch.delenv("ALICE_COOKIES", raising=False)
    from collect_alice_traces import SITE_CUSTOMIZE

    hook_path = tmp_path / "hook.py"
    hook_path.write_text(SITE_CUSTOMIZE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_hook_under_test", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REAL_UUID = "00000000000001610735911788167721"


def test_uuid_in_nested_application_redacted(hook):
    frame = json.dumps({
        "event": {
            "header": {"namespace": "Vins", "name": "TextInput"},
            "payload": {
                "application": {
                    "uuid": REAL_UUID,
                    "device_id": REAL_UUID,
                    "app_id": "ru.yandex.webstandalone.desktop",
                }
            },
        }
    })
    out = hook._safe_frame(frame)
    assert REAL_UUID not in out["payload"], out["payload"][:400]
    parsed = json.loads(out["payload"])
    app = parsed["event"]["payload"]["application"]
    assert app["uuid"] == "[REDACTED]"
    assert app["device_id"] == "[REDACTED]"
    assert app["app_id"] == "ru.yandex.webstandalone.desktop"


def test_uuid_in_synchronize_state_redacted(hook):
    frame = json.dumps({
        "event": {
            "header": {"namespace": "System", "name": "SynchronizeState"},
            "payload": {
                "auth_token": "y0_fake",
                "uuid": REAL_UUID,
                "vins": {"application": {"device_id": REAL_UUID}},
            },
        }
    })
    out = hook._safe_frame(frame)
    assert REAL_UUID not in out["payload"]


def test_uuid_in_client_analytics_redacted(hook):
    frame = json.dumps({
        "event": {
            "header": {"namespace": "Vins", "name": "TextInput"},
            "payload": {
                "application": {"uuid": REAL_UUID},
                "client_analytics_info": {"client_url": "https://alice.yandex.ru/"},
            },
        }
    })
    out = hook._safe_frame(frame)
    assert REAL_UUID not in out["payload"]


def test_non_secret_fields_preserved(hook):
    frame = json.dumps({"name": "keep", "mode": "Base", "value": 42})
    out = hook._safe_frame(frame)
    parsed = json.loads(out["payload"])
    assert parsed == {"name": "keep", "mode": "Base", "value": 42}


def test_redact_for_archive_uuid(hook):
    """redact_for_archive (collector-сторона) тоже должен резать uuid."""
    from collect_alice_traces import redact_for_archive
    out = redact_for_archive({"uuid": REAL_UUID, "keep": "ok"})
    assert out["uuid"] == "[REDACTED]"
    assert out["keep"] == "ok"
