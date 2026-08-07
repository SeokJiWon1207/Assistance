"""Runtime helpers shared by standalone accuracy scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def reexec_in_project_venv(app_dir: Path) -> None:
    """Use the repository virtualenv when a standalone script is run directly."""
    venv_python = app_dir / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return
    if Path(sys.executable).absolute() == venv_python.absolute():
        return
    os.execv(str(venv_python), [str(venv_python), *sys.argv])
