"""Alert manager — re-exported from haloant_kit.

All implementation lives in haloant_kit.alerts. This module exists for
backward compatibility so existing ``from agentgate_backend.alerts import ...`` imports
continue to work.
"""
from haloant_kit.alerts import (  # noqa: F401
    AlertManager,
    INFO,
    WARNING,
    CRITICAL,
)
