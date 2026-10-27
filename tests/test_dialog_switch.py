import pytest

def _session(fake_manager):
    from alice_cli import AliceCliSession
    return AliceCliSession(fake_manager)


def test_handle_command_method_exists(fake_manager):
    s = _session(fake_manager)
    assert hasattr(s, "_handle_command")


def test_switch_no_arg_shows_current(fake_manager, capsys):
    s = _session(fake_manager)
    s.dialog_id = "abc-uuid"
    s._handle_command("switch")
    out = capsys.readouterr().out
    assert "abc-uuid" in out


def test_switch_by_uuid(fake_manager):
    s = _session(fake_manager)
    s.dialog_id = "old"
    fake_manager.known_dialogs.add("target-uuid")
    s._handle_command("switch target-uuid")
    assert s.dialog_id == "target-uuid"


def test_switch_by_index(fake_manager, capsys):
    from alice_cli import AliceCliSession
    s = _session(fake_manager)
    fake_manager.known_dialogs = {"a-uuid", "b-uuid"}
    # /dialogs печатает индексы — смотрим какие
    s._handle_command("dialogs")
    out = capsys.readouterr().out
    # индексы [1] [2] в стабильном порядке
    assert "[1]" in out and "[2]" in out
    s._handle_command("switch 1")
    assert s.dialog_id in {"a-uuid", "b-uuid"}
    s._handle_command("switch 2")
    assert s.dialog_id in {"a-uuid", "b-uuid"}
    assert fake_manager.active_dialog_id == s.dialog_id


def test_switch_unknown_uuid_rejected(fake_manager, capsys):
    s = _session(fake_manager)
    s.dialog_id = "orig"
    s._handle_command("switch nonexistent-uuid")
    # dialog_id не поменялся
    assert s.dialog_id == "orig"
    out = capsys.readouterr().out
    assert "не найден" in out.lower() or "unknown" in out.lower()
