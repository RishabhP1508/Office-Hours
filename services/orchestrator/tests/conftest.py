"""Shared pytest configuration for the orchestrator's test suite.

Makes the top-level `eval` package importable from test files, regardless of how far this tests/
directory sits from wherever `eval/` actually lives on disk. Inside the orchestrator container
(Dockerfile + docker-compose.yml), eval/ is bind-mounted directly under /app as a sibling of app/
and tests/ (docker-compose.yml: `./eval:/app/eval`). In the CI workflow's bare checkout
(.github/workflows/eval.yml), eval/ sits at the real repository root, three directories above
services/orchestrator/tests/. Either way, pytest's own import machinery (prepend import mode, no
__init__.py in this tests/ directory) only ever adds this directory itself to sys.path -- enough to
import `app.*` (installed editable, so it resolves from anywhere) but not `eval.*`. Tests that
exercise eval/run.py's CI-mode logic (test_ci_eval_mode.py) need this.
"""

import sys
from pathlib import Path


def _find_dir_containing_eval_package(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / "eval" / "__init__.py").is_file():
            return candidate
    return None


_root = _find_dir_containing_eval_package(Path(__file__).resolve())
if _root is not None and str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# Shared by test_freshness.py's app.recrawl import-isolation guard and test_ci_eval_mode.py's
# serving-path guard: the predicate for "this sys.modules key names langgraph, langchain, or one
# of their prefixed siblings" (langchain_core, langchain_openai, langgraph_sdk,
# langgraph.checkpoint.sqlite, ...). Both guards build a small standalone script and run it in a
# FRESH subprocess (`python -c <script>`), so there is no live Python object to import into that
# subprocess -- only text. Defining the predicate ONCE, here, as a literal snippet of Python source
# that both guards interpolate verbatim means the two checks cannot independently drift into
# testing two different classes of "langgraph/langchain leaked."
LANGGRAPH_OR_LANGCHAIN_PREDICATE_SRC = "m.startswith('langgraph') or m.startswith('langchain')"
