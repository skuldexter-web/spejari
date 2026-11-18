"""Helpers around the `iw` tool: command execution and output parsing."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from .channels import in_scope


class ToolMissing(Exception):
    pass


def run_cmd(args: List[str], timeout: float = 10.0) -> Tuple[int, str, str]:
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, errors="replace", timeout=timeout, env=env
        )
    except FileNotFoundError as exc:
        raise ToolMissing(f"'{args[0]}' not found - install it (sudo apt install iw)") from exc
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return proc.returncode, proc.stdout, proc.stderr


class LocalExecutor:
    """Runs commands on this machine."""

    label = "local"

    def run(self, args: List[str], timeout: float = 10.0) -> Tuple[int, str, str]:
        return run_cmd(args, timeout)


LOCAL = LocalExecutor()


def parse_iw_dev(text: str) -> List[Dict[str, str]]:
    """Parse `iw dev` into a list of {ifname, phy, type, addr} dicts."""
    out: List[Dict[str, str]] = []
    phy = ""
    cur: Optional[Dict[str, str]] = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"phy#(\d+)", line)
        if m:
            phy, cur = f"phy{m.group(1)}", None
            continue
        if line.startswith("Unnamed/non-netdev"):
            cur = None
            continue
        m = re.match(r"Interface\s+(\S+)", line)
        if m:
            cur = {"ifname": m.group(1), "phy": phy, "type": "", "addr": ""}
            out.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("type "):
            cur["type"] = line.split(None, 1)[1].strip()
        elif line.startswith("addr "):
            cur["addr"] = line.split(None, 1)[1].strip()
    return out


_FREQ_LINE = re.compile(r"^\s*\*\s*(\d+)(?:\.\d+)?\s+MHz\s+\[(\d+)\](.*)$")


def parse_phy_freqs(text: str) -> List[int]:
    """Parse `iw phy <phy> info` into the list of enabled 2.4/5 GHz frequencies."""
    freqs: List[int] = []
    for line in text.splitlines():
        m = _FREQ_LINE.match(line)
        if not m:
            continue
        freq = int(m.group(1))
        if "disabled" in m.group(3):
            continue
        if in_scope(freq):
            freqs.append(freq)
    return sorted(set(freqs))
