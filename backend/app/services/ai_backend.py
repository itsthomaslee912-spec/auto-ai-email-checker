from __future__ import annotations

import asyncio
import heapq
import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass

from openai import AsyncOpenAI
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.crypto import decrypt_token
from app.db import SessionLocal
from app.models import AiSettings

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Ollama (and most local models) serialize GPU work, so every AI classification
# call shares this one gate — whether it comes from live ingest, a bulk sync
# now classifying inline, or a background reclassify pass. Without it, a full
# mailbox import or several mailboxes syncing at once would fire concurrent
# calls and flood the local model, which previously starved /api/health and
# triggered supervisor restarts.
AI_CALL_CONCURRENCY = 1

_PRIORITY_TIER = {"live": 0, "background": 1}


class _PriorityGate:
    """A concurrency-1 gate where "live" callers jump ahead of "background" ones.

    A plain semaphore serves waiters in arrival order, so a live webhook
    email classifying right after a large bulk sync queued thousands of its
    own AI calls would sit behind all of them and lose its real-time feel.
    This gate instead always hands the next free slot to the
    highest-priority waiter, live before background, FIFO within each tier.
    """

    def __init__(self, concurrency: int = 1) -> None:
        self._concurrency = concurrency
        self._in_use = 0
        self._lock = asyncio.Lock()
        self._seq = itertools.count()
        self._waiters: list[tuple[int, int, asyncio.Event]] = []

    async def _acquire(self, tier: int) -> None:
        waiter: tuple[int, int, asyncio.Event] | None = None
        async with self._lock:
            if self._in_use < self._concurrency:
                self._in_use += 1
                return
            event = asyncio.Event()
            waiter = (tier, next(self._seq), event)
            heapq.heappush(self._waiters, waiter)
        try:
            await event.wait()
        except BaseException:
            # Cancellation while queued must not leave a dead waiter in the
            # heap. If ownership was handed to this waiter at the same moment,
            # pass the slot onward (or release it) instead of leaking the only
            # AI slot forever.
            async with self._lock:
                if event.is_set():
                    self._handoff_or_release()
                elif waiter in self._waiters:
                    self._waiters.remove(waiter)
                    heapq.heapify(self._waiters)
            raise

    def _handoff_or_release(self) -> None:
        if self._waiters:
            _, _, event = heapq.heappop(self._waiters)
            event.set()
        else:
            self._in_use -= 1

    async def _release(self) -> None:
        async with self._lock:
            # Hand the slot directly to the next waiter; _in_use is unchanged
            # when ownership transfers rather than becoming free.
            self._handoff_or_release()


_ai_call_gate: _PriorityGate | None = None


def _gate() -> _PriorityGate:
    global _ai_call_gate
    if _ai_call_gate is None:
        _ai_call_gate = _PriorityGate(AI_CALL_CONCURRENCY)
    return _ai_call_gate


@asynccontextmanager
async def ai_call_semaphore(priority: str = "live"):
    """Serialize AI calls behind the shared Ollama-safe gate.

    priority="live" (the default) is for anything a user is waiting on right
    now — a webhook-delivered email, a manual repair action. Pass
    priority="background" for bulk sync and reclassify passes so they never
    delay a live classification that arrives while they're running.
    """
    gate = _gate()
    tier = _PRIORITY_TIER.get(priority, 1)
    await gate._acquire(tier)
    try:
        yield
    finally:
        await gate._release()


@dataclass(frozen=True)
class AiBackend:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None

    def client(self) -> AsyncOpenAI:
        if self.provider == "ollama":
            return AsyncOpenAI(
                api_key="ollama",
                base_url=f"{self.base_url.rstrip('/')}/v1",
                timeout=180.0,
                # The pending-row recovery loop already provides durable
                # retries. SDK retries multiply a single local-model timeout
                # into a nine-minute stall before the worker can fall back.
                max_retries=0,
            )
        if self.provider == "gemini":
            return AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url or GEMINI_OPENAI_BASE_URL,
                timeout=120.0,
            )
        return AsyncOpenAI(api_key=self.api_key)


def get_ai_backend() -> AiBackend:
    defaults = get_settings()
    with SessionLocal() as db:
        saved = db.get(AiSettings, 1)
        if saved and saved.provider == "ollama":
            return AiBackend(
                provider="ollama", model=saved.ollama_model,
                api_key="ollama", base_url=saved.ollama_url,
            )
        if saved and saved.provider == "gemini":
            key = decrypt_token(saved.gemini_key_enc) if saved.gemini_key_enc else defaults.gemini_api_key
            return AiBackend(
                provider="gemini",
                model=saved.gemini_model,
                api_key=key,
                base_url=GEMINI_OPENAI_BASE_URL,
            )
        key = decrypt_token(saved.openai_key_enc) if saved and saved.openai_key_enc else defaults.openai_api_key
        return AiBackend(
            provider="openai",
            model=saved.openai_model if saved else defaults.openai_model,
            api_key=key,
        )


def has_saved_ai_settings() -> bool:
    try:
        with SessionLocal() as db:
            return db.get(AiSettings, 1) is not None
    except OperationalError:
        return False


def get_escalation_backend(active_provider: str) -> AiBackend | None:
    """Return configured OpenAI as an ambiguity-only fallback for local AI."""
    if active_provider != "ollama":
        return None
    defaults = get_settings()
    try:
        with SessionLocal() as db:
            saved = db.get(AiSettings, 1)
            key = decrypt_token(saved.openai_key_enc) if saved and saved.openai_key_enc else defaults.openai_api_key
            model = saved.openai_model if saved else defaults.openai_model
    except OperationalError:
        key, model = defaults.openai_api_key, defaults.openai_model
    return AiBackend("openai", model, key) if key else None
