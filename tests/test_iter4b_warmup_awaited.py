"""warm_up awaited в main_async: race между healthcheck и serve()."""
import asyncio
import inspect
import re
from pathlib import Path

import pytest


def test_warm_up_is_async_def():
    """warm_up должен быть async def, чтобы caller мог await."""
    from tools.gateway import ModelCatalog
    assert inspect.iscoroutinefunction(ModelCatalog.warm_up), (
        "warm_up должен быть async def — иначе caller не может await"
    )


def test_main_async_awaits_warm_up():
    """В main_async должен быть `await catalog.warm_up()`."""
    src = Path("alice_server.py").read_text()
    # Найти блок с catalog.warm_up()
    assert "await catalog.warm_up()" in src, (
        "main_async должен await catalog.warm_up() — иначе race "
        "между healthcheck и server.serve()"
    )
    # И не должно быть fire-and-forget
    assert "catalog.warm_up()" in src
    # Проверим, что не осталось `catalog.warm_up()` без await
    for line in src.splitlines():
        stripped = line.strip()
        if stripped == "catalog.warm_up()" or stripped.startswith("catalog.warm_up()"):
            raise AssertionError(
                f"fire-and-forget вызов catalog.warm_up(): {line!r} — "
                f"должно быть `await catalog.warm_up()`"
            )


async def _warm_up_awaits_healthcheck():
    """warm_up действительно ждёт завершения healthcheck base + pro."""
    from tools.gateway import ModelCatalog
    checks_started = []
    checks_finished = []

    catalog = ModelCatalog(manager_provider=lambda: None)

    async def fake_healthcheck(model_id):
        checks_started.append(model_id)
        await asyncio.sleep(0.05)
        checks_finished.append(model_id)
        catalog.set_status(model_id, "available")
        return catalog.get_state(model_id)

    catalog.healthcheck = fake_healthcheck  # type: ignore

    await catalog.warm_up()

    assert set(checks_started) == {"alice-base", "alice-pro"}, \
        f"warm_up не проверил base+pro: {checks_started}"
    assert set(checks_finished) == {"alice-base", "alice-pro"}, \
        f"warm_up не дождался завершения healthcheck: {checks_finished}"


def test_warm_up_awaits_healthcheck():
    asyncio.run(_warm_up_awaits_healthcheck())


async def _warm_up_models_available_after():
    """После await warm_up() base+pro в статусе available/degraded."""
    from tools.gateway import ModelCatalog
    catalog = ModelCatalog(manager_provider=lambda: None)

    async def fake_healthcheck(model_id):
        catalog.set_status(model_id, "available" if model_id == "alice-base"
                                   else "degraded")
        return catalog.get_state(model_id)

    catalog.healthcheck = fake_healthcheck  # type: ignore
    await catalog.warm_up()

    base = catalog.get_state("alice-base")
    pro = catalog.get_state("alice-pro")
    assert base.status == "available"
    assert pro.status == "degraded"
    # /v1/models должен показать обе
    avail = catalog.list_available()
    assert {s.model_id for s in avail} == {"alice-base", "alice-pro"}


def test_warm_up_models_available_after():
    asyncio.run(_warm_up_models_available_after())


async def _warm_up_handles_healthcheck_error():
    """Ошибка в healthcheck base не ломает warm_up pro."""
    from tools.gateway import ModelCatalog
    calls = []
    catalog = ModelCatalog(manager_provider=lambda: None)

    async def fake_healthcheck(model_id):
        calls.append(model_id)
        if model_id == "alice-base":
            raise RuntimeError("base failed")
        catalog.set_status(model_id, "available")
        return catalog.get_state(model_id)

    catalog.healthcheck = fake_healthcheck  # type: ignore
    await catalog.warm_up()  # не должно бросить
    assert set(calls) == {"alice-base", "alice-pro"}
    assert catalog.get_state("alice-pro").status == "available"


def test_warm_up_handles_healthcheck_error():
    asyncio.run(_warm_up_handles_healthcheck_error())


def test_no_remaining_task_after_warm_up():
    """После await warm_up() нет висящих Task."""
    async def run():
        from tools.gateway import ModelCatalog
        catalog = ModelCatalog(manager_provider=lambda: None)
        async def fake_hc(mid):
            return catalog.get_state(mid)
        catalog.healthcheck = fake_hc  # type: ignore
        await catalog.warm_up()
        # В event loop не должно остаться pending task от warm_up
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        # Ждём немного, чтобы любые fire-and-forget успели завершиться
        await asyncio.sleep(0.01)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return tasks
    leftover = asyncio.run(run())
    assert not leftover, f"warm_up оставил pending task: {leftover}"
