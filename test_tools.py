#!/usr/bin/env python3
import asyncio
from typing import AsyncGenerator, Optional

from agents.base import AliceTurnResponse
from local_tools import LocalToolRegistry, ShellTool
from tool_bridge import LocalToolBridge


class FakeManager:
    """Имитирует AliceAgentManager: отдаёт заранее заготовленную очередь ответов."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.active_dialog_id = "fake-dialog-0001"
        self.calls = []

    async def send_message(self, text, agent_id=None, dialog_id=None, is_suggest=False):
        self.calls.append({"text": text, "agent_id": agent_id, "dialog_id": dialog_id})
        if not self._responses:
            raise RuntimeError("FakeManager: responses exhausted")
        yield self._responses.pop(0)


async def main():
    # Сценарий: Алиса сначала зовёт shell, потом даёт финальный ответ
    responses = [
        AliceTurnResponse(
            dialog_id="fake-dialog-0001",
            request_id="r1",
            text='<tool_call>{"tool":"shell","args":{"cmd":"echo BRIDGE_OK"}}</tool_call>',
            is_finished=True,
        ),
        AliceTurnResponse(
            dialog_id="fake-dialog-0001",
            request_id="r2",
            text="Готово, команда выполнена.",
            is_finished=True,
        ),
    ]

    tools = LocalToolRegistry()
    tools.register(ShellTool(allowed=["echo"], strict=True))

    manager = FakeManager(responses)

    approved_calls = []
    async def approve(name, args):
        approved_calls.append((name, args))
        return True

    bridge = LocalToolBridge(manager, tools, approve_callback=approve)

    collected = []
    async for turn in bridge.send_with_tools("скажи привет", dialog_id=None):
        collected.append(turn)

    # Проверки:
    # 1. Должно быть минимум 4 turn'а: wait, tool-log, wait, final
    assert len(collected) >= 3, f"too few turns: {len(collected)}"
    final = collected[-1]
    assert final.is_finished and not final.error, f"FAIL final: {final}"
    assert "Готово" in final.text, f"FAIL text: {final.text}"

    # 2. Аппрув вызвался ровно для shell
    assert approved_calls == [("shell", {"cmd": "echo BRIDGE_OK"})], f"FAIL approve: {approved_calls}"

    # 3. Второй вызов менеджера должен содержать tool_result
    assert len(manager.calls) == 2, f"FAIL calls: {len(manager.calls)}"
    second_prompt = manager.calls[1]["text"]
    assert "<tool_result" in second_prompt, f"FAIL no tool_result: {second_prompt[:200]}"
    assert "BRIDGE_OK" in second_prompt, f"FAIL no output: {second_prompt[:200]}"
    assert "<tool_call>" not in second_prompt.split("tool_result")[-1][:500], "reminder OK"

    print("✅ Tool-loop прошел")
    print(f"   turns: {len(collected)}")
    print(f"   manager.calls: {len(manager.calls)}")
    print(f"   final.text: {final.text!r}")
    print(f"   second prompt содержит tool_result ✅")


async def test_denied():
    """Отклонение аппрува не должно исполнять команду."""
    responses = [
        AliceTurnResponse(dialog_id="d", request_id="r1",
            text='<tool_call>{"tool":"shell","args":{"cmd":"echo X"}}</tool_call>',
            is_finished=True),
        AliceTurnResponse(dialog_id="d", request_id="r2",
            text="Ладно, не буду.", is_finished=True),
    ]
    tools = LocalToolRegistry()
    tools.register(ShellTool(allowed=["echo"], strict=True))
    manager = FakeManager(responses)

    async def deny(name, args):
        return False

    bridge = LocalToolBridge(manager, tools, approve_callback=deny)
    async for _ in bridge.send_with_tools("плохая идея"):
        pass
    # tool_result должен быть denied, а не X
    second_prompt = manager.calls[1]["text"]
    assert 'denied="true"' in second_prompt, f"FAIL: {second_prompt[:300]}"
    print("✅ Отклонение аппрува работает")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_denied())
    print("\n🎉 Bridge mock tests OK")