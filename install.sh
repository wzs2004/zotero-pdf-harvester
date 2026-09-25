#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "错误：请先安装 Python 3.10+"; exit 1; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("错误：需要 Python 3.10+")
PY

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e '.[full]'
.venv/bin/pip install pytest
# macOS 优先使用已安装的 Google Chrome，避免额外下载约 180 MB Chromium。
if [ "$(uname -s)" != "Darwin" ] || [ ! -d "/Applications/Google Chrome.app" ]; then
  .venv/bin/playwright install chromium
fi
[ -f .env ] || cp .env.example .env

echo
echo "安装完成。请编辑 .env 填写 ZPH_EMAIL，然后运行："
echo "  ./start-gui.sh"
