"""Frozen client entry point; server modules are deliberately not imported."""

import sys
from pathlib import Path

if Path(sys.argv[0]).name == "pr-rerun-update":
    from client_install import main
else:
    from pr_rerun.cli import main

if __name__ == "__main__":
    main()
