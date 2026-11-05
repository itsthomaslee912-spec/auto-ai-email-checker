from __future__ import annotations

from app.classify.prompt import SYSTEM_PROMPT, with_interview_subtype_rules

_cached_prompt: str | None = None
_cached_normalized_prompt: str | None = None


def invalidate_prompt_cache() -> None:
    global _cached_prompt, _cached_normalized_prompt
    _cached_prompt = None
    _cached_normalized_prompt = None


def activate_prompt_text(text: str) -> None:
    global _cached_prompt, _cached_normalized_prompt
    _cached_prompt = text
    _cached_normalized_prompt = None


def _load_from_db() -> str | None:
    from app.db import SessionLocal
    from app.models import ClassifyPromptVersion

    db = SessionLocal()
    try:
        row = (
            db.query(ClassifyPromptVersion)
            .filter(ClassifyPromptVersion.is_active.is_(True))
            .order_by(ClassifyPromptVersion.id.desc())
            .first()
        )
        if row is None:
            return None
        text = (row.prompt_text or "").strip()
        return text or None
    except Exception:
        return None
    finally:
        db.close()


def get_active_system_prompt() -> str:
    """Return the live classify prompt: in-memory cache, else SQLite, else seed."""
    global _cached_prompt
    if _cached_prompt and _cached_prompt.strip():
        return _cached_prompt
    text = _load_from_db() or SYSTEM_PROMPT
    _cached_prompt = text
    return text


def get_normalized_active_prompt() -> str:
    """Return the active prompt with all runtime patches applied, cached.

    ``with_interview_subtype_rules`` performs several string scans and
    replacements on every classification call. The result is deterministic for a
    given prompt, so it is cached here and invalidated alongside the raw prompt.
    """
    global _cached_normalized_prompt
    if _cached_normalized_prompt:
        return _cached_normalized_prompt
    _cached_normalized_prompt = with_interview_subtype_rules(get_active_system_prompt())
    return _cached_normalized_prompt
