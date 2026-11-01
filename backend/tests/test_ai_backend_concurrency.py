from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services import ai_backend


def _reset_gates() -> None:
    ai_backend._ai_call_gate = None
    ai_backend._ai_call_gates.clear()


def test_provider_concurrency_uses_separate_cloud_and_ollama_limits(monkeypatch):
    monkeypatch.setattr(
        ai_backend,
        "get_settings",
        lambda: SimpleNamespace(ai_cloud_concurrency=4, ai_ollama_concurrency=1),
    )
    _reset_gates()

    assert ai_backend.ai_call_concurrency("openai") == 4
    assert ai_backend.ai_call_concurrency("gemini") == 4
    assert ai_backend.ai_call_concurrency("ollama") == 1


def test_cloud_gate_allows_parallel_calls_while_ollama_stays_serial(monkeypatch):
    monkeypatch.setattr(
        ai_backend,
        "get_settings",
        lambda: SimpleNamespace(ai_cloud_concurrency=4, ai_ollama_concurrency=1),
    )

    async def peak_for(provider: str) -> int:
        active = 0
        peak = 0

        async def run_one() -> None:
            nonlocal active, peak
            async with ai_backend.ai_call_semaphore("background", provider=provider):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(run_one() for _ in range(8)))
        return peak

    _reset_gates()
    assert asyncio.run(peak_for("openai")) == 4
    _reset_gates()
    assert asyncio.run(peak_for("ollama")) == 1


def test_provider_gates_do_not_block_each_other(monkeypatch):
    monkeypatch.setattr(
        ai_backend,
        "get_settings",
        lambda: SimpleNamespace(ai_cloud_concurrency=2, ai_ollama_concurrency=1),
    )
    _reset_gates()

    async def run() -> None:
        async with ai_backend.ai_call_semaphore(provider="ollama"):
            await asyncio.wait_for(
                _enter_cloud_gate(),
                timeout=0.1,
            )

    async def _enter_cloud_gate() -> None:
        async with ai_backend.ai_call_semaphore(provider="openai"):
            return

    asyncio.run(run())
