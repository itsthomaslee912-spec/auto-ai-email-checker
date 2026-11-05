from __future__ import annotations

import pytest

from app.classify.prompt_store import invalidate_prompt_cache
from app.services.ai_backend import invalidate_ai_backend_cache


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Clear module-level caches before every test.

    get_ai_client and get_ai_backend cache their results at the module level so
    that production AI calls reuse connection pools. Without resetting between
    tests, a client created with real (or no) credentials in one test bleeds
    into the next, causing AuthenticationError or silently bypassing mocks.
    The same applies to the normalized-prompt cache.
    """
    invalidate_ai_backend_cache()
    invalidate_prompt_cache()
    yield
    invalidate_ai_backend_cache()
    invalidate_prompt_cache()
