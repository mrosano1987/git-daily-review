"""
_bootstrap.py — interpreter bootstrap for the command-line entry points.

The engine needs PyYAML. A bare `python3` on the user's PATH is whatever the
OS or Homebrew happens to point at that week, and a Python upgrade silently
drops previously installed site-packages — which turns every documented
`python3 scripts/daily_review.py` into a raw ModuleNotFoundError.

So: if the current interpreter cannot import yaml, re-exec the same command
under the project virtualenv (or $GDR_PYTHON) when one is usable, and
otherwise fail with instructions instead of a traceback.

Entry points call ensure_deps() before importing yaml.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# Set while re-execing, so a broken venv cannot cause an exec loop.
_GUARD = "GDR_BOOTSTRAP_REEXEC"

SETUP_HINT = f"""\
Missing Python dependency: PyYAML (running under {sys.executable}).

Create the project virtualenv once, then re-run the same command:

    python3 -m venv {ROOT_DIR}/.venv
    {ROOT_DIR}/.venv/bin/pip install -r {ROOT_DIR}/requirements.txt

The entry points pick up .venv automatically. To point at a different
interpreter instead, export GDR_PYTHON=/path/to/python.\
"""


def _has_yaml(python: Path) -> bool:
    """True if `python` can import yaml."""
    try:
        return subprocess.run(
            [str(python), "-c", "import yaml"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _candidates():
    """Interpreters to try, in order of preference."""
    override = os.environ.get("GDR_PYTHON")
    if override:
        yield Path(override)
    for venv in (".venv", "venv"):
        for name in ("python3", "python"):
            yield ROOT_DIR / venv / "bin" / name          # POSIX
            yield ROOT_DIR / venv / "Scripts" / f"{name}.exe"  # Windows


def ensure_deps():
    """Re-exec under an interpreter that has PyYAML, or exit with guidance."""
    try:
        import yaml  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    if os.environ.get(_GUARD):
        # Already re-execed once and yaml is still missing — do not loop.
        sys.exit(SETUP_HINT)

    current = Path(sys.executable).resolve()
    for candidate in _candidates():
        if not candidate.is_file() or candidate.resolve() == current:
            continue
        if not _has_yaml(candidate):
            continue
        os.environ[_GUARD] = "1"
        try:
            os.execv(str(candidate), [str(candidate), *sys.argv])
        except OSError:
            del os.environ[_GUARD]
            continue

    sys.exit(SETUP_HINT)
