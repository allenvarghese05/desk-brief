"""Cross-checks (spec §8.2). Import this package to get the registry and the runner.

Every module in the package registers its check on import; ``run_crosschecks`` loads them all,
so a new check module only needs to call ``register`` at import time.
"""

from deskbrief.crosschecks.base import (
    CrossCheck,
    register,
    registered,
    run_crosschecks,
    to_dq_result,
)

__all__ = ["CrossCheck", "register", "registered", "run_crosschecks", "to_dq_result"]
