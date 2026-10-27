from tools.gateway import _extract_tool_calls


def test_known_tool_call():
    text = '<tool_call>{"tool": "my_func", "args": {"x": 1}}</tool_call>'
    calls = _extract_tool_calls(text, [{"function": {"name": "my_func"}}])
    assert len(calls) == 1
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "my_func"
    assert calls[0]["function"]["arguments"] == '{"x": 1}'


def test_unknown_tool_ignored():
    text = '<tool_call>{"tool": "other", "args": {}}</tool_call>'
    calls = _extract_tool_calls(text, [{"function": {"name": "my_func"}}])
    assert calls == []


def test_bad_json_ignored():
    text = '<tool_call>not-json</tool_call>'
    calls = _extract_tool_calls(text, [{"function": {"name": "my_func"}}])
    assert calls == []


def test_no_tools_declared_means_no_calls():
    text = '<tool_call>{"tool": "my_func", "args": {}}</tool_call>'
    calls = _extract_tool_calls(text, [])
    assert calls == []


def test_tool_choice_none_semantics():
    # если tools == [] после tool_choice=none, то _extract_tool_calls не найдёт ни одного
    text = '<tool_call>{"tool": "my_func", "args": {}}</tool_call>'
    calls = _extract_tool_calls(text, [])
    assert calls == []
