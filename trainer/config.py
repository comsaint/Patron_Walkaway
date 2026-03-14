# Re-export from trainer.core.config for backward compatibility (PLAN 項目 2.2).
# All existing "from trainer.config import ..." continue to work.
from trainer.core.config import *  # noqa: F401, F403
# _REPO_ROOT is not re-exported by "import *"; expose for tests and any direct access.
from trainer.core.config import _REPO_ROOT  # noqa: F401
