import json
from pathlib import Path

def test_parse_nsb_card_importable_from_tools_parser():
    from tools.parser import parse_nsb_card  # noqa: F401


def test_rich_uicard_unknown_does_not_raise():
    from tools.parser import parse_nsb_card
    # пустой card — не должно бросать
    text, steps, sources = parse_nsb_card({})
    assert text == ""
    assert steps == []
    assert sources == []
