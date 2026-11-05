from __future__ import annotations

import asyncio
import heapq
import inspect
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

# Legacy default retained for test and third-party imports. Runtime gates use
# provider-specific settings via ``ai_call_concurrency`` below.
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
_ai_call_gates: dict[str, _PriorityGate] = {}

_cached_backend: "AiBackend | None" = None
_cached_client: AsyncOpenAI | None = None
_has_saved_settings: bool | None = None


def invalidate_ai_backend_cache() -> None:
    """Call after saving AI settings so the next classification sees the update."""
    global _cached_backend, _cached_client, _has_saved_settings
    _cached_backend = None
    _cached_client = None
    _has_saved_settings = None


def ai_call_concurrency(provider: str) -> int:
    settings = get_settings()
    configured = settings.ai_ollama_concurrency if provider == "ollama" else (
        settings.ai_cloud_concurrency if provider in {"openai", "gemini"} else AI_CALL_CONCURRENCY
    )
    return max(1, min(int(configured), 32))


def _gate(provider: str) -> _PriorityGate:
    global _ai_call_gate
    # Tests and maintenance helpers historically reset this sentinel directly.
    # Preserve that contract while keeping independent provider gates.
    if _ai_call_gate is None:
        _ai_call_gates.clear()
    gate = _ai_call_gates.get(provider)
    if gate is None:
        gate = _PriorityGate(ai_call_concurrency(provider))
        _ai_call_gates[provider] = gate
    _ai_call_gate = gate
    return gate


@asynccontextmanager
async def ai_call_semaphore(priority: str = "live", provider: str = "ollama"):
    """Limit AI calls with a provider-specific, priority-aware gate.

    priority="live" (the default) is for anything a user is waiting on right
    now — a webhook-delivered email, a manual repair action. Pass
    priority="background" for bulk sync and reclassify passes so they never
    delay a queued live classification. Ollama remains serialized while cloud
    providers can use multiple configured request slots.
    """
    gate = _gate(provider)
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
    global _cached_backend
    if _cached_backend is not None:
        return _cached_backend
    defaults = get_settings()
    with SessionLocal() as db:
        saved = db.get(AiSettings, 1)
        if saved and saved.provider == "ollama":
            _cached_backend = AiBackend(
                provider="ollama", model=saved.ollama_model,
                api_key="ollama", base_url=saved.ollama_url,
            )
        elif saved and saved.provider == "gemini":
            key = decrypt_token(saved.gemini_key_enc) if saved.gemini_key_enc else defaults.gemini_api_key
            _cached_backend = AiBackend(
                provider="gemini",
                model=saved.gemini_model,
                api_key=key,
                base_url=GEMINI_OPENAI_BASE_URL,
            )
        else:
            key = decrypt_token(saved.openai_key_enc) if saved and saved.openai_key_enc else defaults.openai_api_key
            _cached_backend = AiBackend(
                provider="openai",
                model=saved.openai_model if saved else defaults.openai_model,
                api_key=key,
            )
    return _cached_backend


def has_saved_ai_settings() -> bool:
    global _has_saved_settings
    if _has_saved_settings is not None:
        return _has_saved_settings
    try:
        with SessionLocal() as db:
            result = db.get(AiSettings, 1) is not None
        _has_saved_settings = result
        return result
    except OperationalError:
        return False


def get_ai_client(backend: AiBackend) -> AsyncOpenAI:
    """Return a long-lived client for the current backend, reusing the connection pool.

    The cached client is shared across AI calls for the same backend
    configuration. Call ``invalidate_ai_backend_cache`` when settings change to
    get a fresh client on the next request.
    """
    global _cached_client
    if _cached_client is None:
        _cached_client = backend.client()
    return _cached_client


async def close_ai_client(client: object) -> None:
    """Close a per-request SDK client without constraining test doubles.

    The long-lived cached client is not closed here — it is reused across calls.
    One-off clients (test doubles, temporary overrides) are closed normally.
    """
    if client is _cached_client:
        return
    close = getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result
