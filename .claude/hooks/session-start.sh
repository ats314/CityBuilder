#!/bin/bash
# Install what tools/ needs so tests and bakes work without a manual setup step.
# Web sessions only: locally you manage your own environment.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# Idempotent, and deliberately `install` rather than a locked sync so the cached
# container state is reused on later sessions.
pip install --quiet --disable-pip-version-check \
  -r requirements.txt -r requirements-dev.txt

# Chromium is already present in this image and PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD
# is set, so `playwright install` must not run. Point the smoke test at the
# preinstalled binary instead, globbing the build number rather than pinning it.
chrome=$(ls -d /opt/pw-browsers/chromium-*/chrome-linux/chrome 2>/dev/null | head -1 || true)
if [ -n "$chrome" ] && [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export SMOKE_CHROME=\"$chrome\"" >> "$CLAUDE_ENV_FILE"
fi

echo "deps ready; verify with: python3 tools/smoke_test.py"
