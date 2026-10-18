import asyncio

from app.services.ai_backend import _PriorityGate


def test_cancelled_waiter_does_not_leak_gate_slot():
    async def scenario() -> None:
        gate = _PriorityGate(1)
        await gate._acquire(1)

        cancelled = asyncio.create_task(gate._acquire(1))
        await asyncio.sleep(0)
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass

        await gate._release()
        await asyncio.wait_for(gate._acquire(0), timeout=0.1)
        await gate._release()

    asyncio.run(scenario())


def test_cancel_during_handoff_passes_slot_to_next_waiter():
    async def scenario() -> None:
        gate = _PriorityGate(1)
        await gate._acquire(1)

        cancelled = asyncio.create_task(gate._acquire(1))
        next_waiter = asyncio.create_task(gate._acquire(1))
        await asyncio.sleep(0)

        await gate._release()
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass

        await asyncio.wait_for(next_waiter, timeout=0.1)
        await gate._release()

    asyncio.run(scenario())
