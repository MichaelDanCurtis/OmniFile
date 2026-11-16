#!/usr/bin/env bash
# Run the Windows client kit's Pester tests, on any machine with PowerShell 7 (pwsh): Windows-only
# cmdlets are mocked, so the tests check the runners' logic and their output.
#
#   tests/windows/run-tests.sh
#
# Uses Pester 5.7.1, pinned: from the PowerShell Gallery into ~/.cache/omnifile/pester the first time
# (Save-Module; nothing is installed system-wide), then from there. Exits non-zero if any test fails.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
PESTER_VERSION=5.7.1
modules="${XDG_CACHE_HOME:-$HOME/.cache}/omnifile/pester"
command -v pwsh >/dev/null || { echo "run-tests.sh: needs PowerShell 7 (pwsh)" >&2; exit 2; }
if [ ! -f "$modules/Pester/$PESTER_VERSION/Pester.psd1" ]; then
  mkdir -p "$modules"
  pwsh -NoProfile -NonInteractive -Command \
    "Save-Module -Name Pester -RequiredVersion $PESTER_VERSION -Repository PSGallery -Path '$modules'"
fi
export PSModulePath="$modules${PSModulePath:+:$PSModulePath}"
exec pwsh -NoProfile -NonInteractive -File "$here/tests/Invoke-Tests.ps1" -PesterVersion "$PESTER_VERSION"
