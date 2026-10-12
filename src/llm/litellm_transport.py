"""Load LiteLLM only when a provider completion is actually requested.

API construction and schema tooling import the LLM facade without initializing
provider SDKs or tokenizer caches. Keep the shared, patchable completion seam.
"""

from __future__ import annotations

from typing import Any


def completion(*args: Any, **kwargs: Any) -> Any:
    """Delegate unchanged arguments, results and exceptions to real LiteLLM."""
    import litellm

    # Preserve the facade's quiet provider logging at the execution boundary.
    litellm.suppress_debug_info = True
    return litellm.completion(*args, **kwargs)
