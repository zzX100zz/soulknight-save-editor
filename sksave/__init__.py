"""Soul Knight (iOS) save editor built on the AirLift sandbox escape."""

from .fields import BUNDLE_ID, GEM_MAX, MAX_QUANTITY, SEASON_COIN_MAX
from .patcher import UnlockOptions, apply, describe_state
from .session import SaveSession, connect
from .workspace import Catalog, SaveWorkspace

__version__ = "1.0.0"
__all__ = [
    "BUNDLE_ID",
    "GEM_MAX",
    "MAX_QUANTITY",
    "SEASON_COIN_MAX",
    "Catalog",
    "SaveSession",
    "SaveWorkspace",
    "UnlockOptions",
    "apply",
    "connect",
    "describe_state",
    "__version__",
]
