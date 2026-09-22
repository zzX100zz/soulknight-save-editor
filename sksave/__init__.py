"""Soul Knight (iOS) save editor built on the AirLift sandbox escape.

Importing this package stays cheap on purpose: ``sksave.web`` must be able to
start on a bare system Python (before the virtualenv exists), and only the save
crypto pulls in third-party modules.  Names are therefore resolved lazily.
"""

from __future__ import annotations

from typing import Any

__version__ = "1.0.0"

_LAZY = {
    "BUNDLE_ID": ("sksave.fields", "BUNDLE_ID"),
    "GEM_MAX": ("sksave.fields", "GEM_MAX"),
    "MAX_QUANTITY": ("sksave.fields", "MAX_QUANTITY"),
    "SEASON_COIN_MAX": ("sksave.fields", "SEASON_COIN_MAX"),
    "Catalog": ("sksave.workspace", "Catalog"),
    "SaveWorkspace": ("sksave.workspace", "SaveWorkspace"),
    "SaveSession": ("sksave.session", "SaveSession"),
    "UnlockOptions": ("sksave.patcher", "UnlockOptions"),
    "apply": ("sksave.patcher", "apply"),
    "connect": ("sksave.session", "connect"),
    "describe_state": ("sksave.patcher", "describe_state"),
}

__all__ = ["__version__", *_LAZY]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _LAZY[name]
    except KeyError as error:  # pragma: no cover - normal attribute error
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    from importlib import import_module

    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
