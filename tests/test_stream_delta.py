import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures"


def test_parser_compute_delta_exists():
    from tools.parser import compute_delta  # noqa: F401


def test_snapshot_stream_sum_equals_final():
    """Реальная последовательность из A5 (ref 5c9a) — накопительный снимок."""
    from tools.parser import compute_delta
    frames = json.loads((FIX / "deferred_real_snapshot.json").read_text())
    prev = ""
    deltas = []
    for f in frames:
        d, prev = compute_delta(prev, f["text"])
        deltas.append(d)
    assert prev == frames[-1]["text"]
    assert "".join(deltas) == frames[-1]["text"]
    # сумма длин дельт == длина финального текста
    assert sum(len(d) for d in deltas) == len(frames[-1]["text"])


def test_true_delta_stream():
    """Сервер шлёт настоящие дельты — не должны теряться."""
    from tools.parser import compute_delta
    frames = json.loads((FIX / "deferred_true_delta.json").read_text())
    prev = ""
    deltas = []
    for f in frames:
        d, prev = compute_delta(prev, f["text"])
        deltas.append(d)
    assert "".join(deltas) == "Hello world"
    assert prev == "Hello world"


def test_identical_frame_is_empty_delta():
    from tools.parser import compute_delta
    d, prev = compute_delta("hello", "hello")
    assert d == ""
    assert prev == "hello"
