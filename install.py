#!/usr/bin/env python3
"""One install command:   python3 install.py   (runs ./setup.sh; extra flags are passed through)

Spejari runs on Debian, Ubuntu, Kali and Raspberry Pi OS.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("Spejari runs on Linux (Debian, Ubuntu, Kali, Raspberry Pi OS). This looks like", sys.platform)
        return 1
    script = ROOT / "setup.sh"
    os.chmod(script, 0o755)
    return subprocess.call([str(script)] + sys.argv[1:], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
