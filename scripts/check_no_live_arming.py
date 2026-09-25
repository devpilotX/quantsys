#!/usr/bin/env python
"""Refuse to commit a configuration that arms live trading.

This repository's research is closed with a documented no-edge verdict, the
live gate is deliberately shut, and arming it requires `QS_LIVE_ARMED=1` in the
environment. That flag is meant to be set by hand on the box, by a human who
has just rotated credentials and read GOLIVE.md — never to arrive as part of a
commit, a template, a compose file, or a systemd unit.

The failure this guards against is mundane and expensive: someone flips the
flag locally to test something, forgets, and commits it. Nothing else in the
chain would object, because every other lock (operator re-auth, typed phrase,
backtest gate) sits downstream of this one being set.

Exit non-zero on any assignment of QS_LIVE_ARMED to a truthy value.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# QS_LIVE_ARMED=1 / : 1 / ="true" / - QS_LIVE_ARMED=yes, with or without quotes,
# export prefixes, or YAML list syntax.
PATTERN = re.compile(
    r"""QS_LIVE_ARMED \s* [:=] \s* ["']? \s* (1|true|yes|on) \b""",
    re.IGNORECASE | re.VERBOSE,
)

# A line may opt out when it is demonstrably documentation rather than config.
ALLOW_MARKER = "pragma: allow-live-arming-example"


def check(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        if PATTERN.search(line):
            problems.append(f"{path}:{lineno}: {line.strip()}")
    return problems


def main(argv: list[str]) -> int:
    found: list[str] = []
    for arg in argv:
        found.extend(check(Path(arg)))
    if not found:
        return 0
    print("refusing the commit: live trading would be armed by configuration\n")
    for f in found:
        print(f"  {f}")
    print(
        "\nQS_LIVE_ARMED must only ever be set by hand on the deployment host,\n"
        "after credential rotation, per docs/GOLIVE.md. If this line really is\n"
        f"documentation, append:  # {ALLOW_MARKER}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
