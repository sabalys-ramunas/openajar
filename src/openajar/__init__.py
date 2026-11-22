"""openajar: a self-hosted computer for your agents."""

import os as _os

__version__ = "0.5.4"

LEGACY_ENV_PREFIX = "AGENTOS_"  # legacy-name: the variables' prefix before the rename (docs/LEGACY-NAMES.md)


def _legacy_env() -> None:
    """Every variable set under the old prefix counts as its OPENAJAR_ one when that one is not set: an
    OPENAJAR_ variable wins, and the old one is read otherwise (in this process and the ones it starts)."""
    for key, value in list(_os.environ.items()):
        if key.startswith(LEGACY_ENV_PREFIX):
            _os.environ.setdefault("OPENAJAR_" + key[len(LEGACY_ENV_PREFIX):], value)


_legacy_env()
