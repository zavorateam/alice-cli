"""Инвариант _ToolMarkerFilter: streaming == non-stream."""
import pytest

from tools.gateway import _ToolMarkerFilter, _clean_tool_markers


def test_filter_no_marker_passthrough():
    f = _ToolMarkerFilter()
    assert f.feed("hello world") == "hello world"
    assert f.flush() == ""


def test_filter_simple_marker():
    f = _ToolMarkerFilter()
    out = f.feed("Pre <tool_call>{\"x\":1}</tool_call> Post")
    out += f.flush()
    assert out == "Pre  Post"
    assert "<tool_call>" not in out


def test_filter_split_across_feeds():
    f = _ToolMarkerFilter()
    out = ""
    out += f.feed("Pre <tool_")
    out += f.feed("call>{\"a\":")
    out += f.feed("1}</tool_")
    out += f.feed("call> Post")
    out += f.flush()
    assert "Pre" in out
    assert "Post" in out
    assert "<tool_call>" not in out
    assert "</tool_call>" not in out


def test_filter_unclosed_marker_dropped():
    f = _ToolMarkerFilter()
    out = f.feed("Pre <tool_call>some content without close")
    out += f.flush()
    assert "<tool_call>" not in out
    # содержимое ушло, Pre осталось
    assert "Pre" in out


def test_filter_malformed_marker_stripped():
    """Маркер без JSON — тоже strip, как в streaming, так и в _clean_tool_markers."""
    text = "A <tool_call>not json</tool_call> B"
    assert _clean_tool_markers(text) == "A  B"


def test_filter_equivalence_streaming_vs_full():
    """feed(t1)+feed(t2)+...+flush() == _clean_tool_markers(t1+t2+...)."""
    cases = [
        ["hello"],
        ["Pre ", "<tool_call>{}</tool_call>", " Post"],
        ["A <tool_ca", "ll>{", "\"x\":1}", "</tool_", "call> B"],
        ["<tool_call>unclosed", " content"],
        ["no markers at all"],
        ["<tool_call>a</tool_call><tool_call>b</tool_call>"],
    ]
    for chunks in cases:
        full = "".join(chunks)
        # streaming
        f = _ToolMarkerFilter()
        stream_out = "".join(f.feed(c) for c in chunks) + f.flush()
        # full
        full_out = _clean_tool_markers(full)
        assert stream_out == full_out, (
            f"mismatch for {chunks}: stream={stream_out!r} full={full_out!r}"
        )


def test_filter_no_marker_no_side_effects():
    """Текст без маркеров проходит полностью через feed+flush."""
    text = "Просто текст без маркеров."
    f = _ToolMarkerFilter()
    out = f.feed(text) + f.flush()
    assert out == text


def test_filter_leading_partial_tag_held():
    """Если feed заканчивается на <, следующий feed продолжает тег."""
    f = _ToolMarkerFilter()
    o1 = f.feed("text <")
    # "<" остался в буфере (может стать <tool_call>)
    o2 = f.feed("b")  # "<" + "b" = "<b" — не <tool_, отдаём всё
    o3 = f.feed("old")
    out = o1 + o2 + o3 + f.flush()
    assert out == "text <bold"


def test_clean_tool_markers_no_strip():
    """_clean_tool_markers не делает .strip() — консистентно с потоком."""
    assert _clean_tool_markers("  hello  ") == "  hello  "
    assert _clean_tool_markers("  a<tool_call>{}</tool_call>b  ") == "  ab  "
