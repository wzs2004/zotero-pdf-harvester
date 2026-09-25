#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/zotero-pdf-harvester-gui ] || { echo "请先运行 ./install.sh"; exit 1; }
exec .venv/bin/zotero-pdf-harvester-gui "$@"
