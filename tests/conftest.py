import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeWs:
    def __init__(self):
        self.update_subscriptions = self._async_noop
        self.expert_trial_left = None
        self.expert_trial_limit = None

    async def _async_noop(self, *a, **kw):
        return None

    async def upload_file(self, *a, **kw):
        return None


class FakeManager:
    def __init__(self):
        self.active_dialog_id = "dialog-initial"
        self.known_dialogs = {"dialog-initial"}
        self.ephemeral_dialogs = set()
        self.ws = FakeWs()
        self.dialog_prev_req_ids = {}

    async def create_new_dialog(self, agent_id=None, is_ephemeral=False):
        new_id = "dialog-new-" + str(len(self.known_dialogs))
        self.known_dialogs.add(new_id)
        self.active_dialog_id = new_id
        if is_ephemeral:
            self.ephemeral_dialogs.add(new_id)
        return new_id

    async def send_message(self, *a, **kw):
        if False:
            yield
        return

    async def shutdown(self):
        return None

    def __getattr__(self, name):
        # прочие атрибуты — возвращаем заглушку
        async def _noop(*a, **kw):
            return None
        return _noop


@pytest.fixture
def fake_manager():
    return FakeManager()
