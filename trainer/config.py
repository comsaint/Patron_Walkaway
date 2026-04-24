# Stable compatibility facade over ``trainer.core.config``.
# All existing ``from trainer.config import ...`` imports continue to work even as
# internal config sections move into dedicated implementation shards.
from trainer.core.config import *  # noqa: F401, F403
# _REPO_ROOT is not re-exported by "import *"; expose for tests and any direct access.
from trainer.core.config import _REPO_ROOT  # noqa: F401
