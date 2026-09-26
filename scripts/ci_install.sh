#!/usr/bin/env bash
# Install the engine for CI at a reproducible point, or deliberately not.
#
# Usage: scripts/ci_install.sh <python-minor>      e.g. scripts/ci_install.sh 3.14
#
# push, pull_request, workflow_dispatch
#     Install with constraints/linux-py<minor>.txt. A red build then means the
#     code changed, not that PyPI moved underneath it.
#
# schedule (the weekly run)
#     Install unpinned. Upstream drift has to surface somewhere, and a
#     scheduled run is the place for it; the same failure inside an unrelated
#     pull request is noise.
#
# The pins are regenerated per Python minor by resolving in the matching
# python:<minor>-slim image; the command is in the header of each file.
set -euo pipefail

minor="${1:?usage: ci_install.sh <python-minor>, e.g. 3.14}"
constraints="constraints/linux-py${minor}.txt"
event="${GITHUB_EVENT_NAME:-push}"

if [[ "$event" == "schedule" ]]; then
  echo "::notice title=Unpinned install::weekly drift run, ignoring ${constraints}"
  python -m pip install -e ".[dev,broker]"
  exit 0
fi

if [[ ! -f "$constraints" ]]; then
  echo "::error title=Missing constraints::${constraints} does not exist." >&2
  echo "Generate it with the command in any constraints/*.txt header." >&2
  exit 1
fi

python -m pip install -e ".[dev,broker]" -c "$constraints"

# A constraint that pins a transitive below what another package declares
# resolves cleanly and then fails at import; pip check catches it here.
python -m pip check
