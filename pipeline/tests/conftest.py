"""Make the tests import *this* checkout's pipeline, not an installed one.

``[tool.pytest.ini_options] pythonpath = ["src"]`` inserts the source directory at
the front of ``sys.path``, which is enough on a clean machine. It is not enough
when the package is also pip-installed from a *different* checkout -- a git
worktree, or a second clone -- because the install's ``.pth`` entry names an
absolute path and whichever one Python reaches first wins. That failure is silent
and total: the tests pass, and they pass against somebody else's code.

So the path is pinned here, where it is relative to this file and cannot point
anywhere else, and any already-imported ``wowdps`` from elsewhere is dropped so
the next import re-resolves.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

if str(SRC) in sys.path:
    sys.path.remove(str(SRC))
sys.path.insert(0, str(SRC))

for name in [name for name in sys.modules if name == "wowdps" or name.startswith("wowdps.")]:
    module = sys.modules[name]
    origin = getattr(module, "__file__", None)
    if origin and not str(Path(origin).resolve()).startswith(str(SRC)):
        del sys.modules[name]
