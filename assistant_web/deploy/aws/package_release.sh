#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" != "2" ]]; then
  echo "Usage: package_release.sh SOURCE_ROOT RELEASE_DIR" >&2
  exit 1
fi
SOURCE_ROOT="$(cd "$1" && pwd)"
RELEASE_DIR="$2"
VERIFICATION_FILES=(
  .coveragerc
  .python-version
  pyproject.toml
  .github/workflows/ci.yml
  scripts/make_vector_db.py
)
for relative in "${VERIFICATION_FILES[@]}"; do
  if [[ ! -f "$SOURCE_ROOT/$relative" ]]; then
    echo "Required release verification file is missing: $relative" >&2
    exit 1
  fi
done

mkdir -p "$RELEASE_DIR/assistant_web"
rsync -az --delete \
  --exclude '.DS_Store' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.pem' \
  --exclude 'firebase-service-account*.json' \
  --exclude '*service-account*.json' \
  --exclude '.venv' \
  --exclude '.pycache' \
  --exclude 'artifacts' \
  --exclude 'runtime/' \
  --exclude '.env' \
  "$SOURCE_ROOT/assistant_web/" "$RELEASE_DIR/assistant_web/"
for relative in "${VERIFICATION_FILES[@]}"; do
  mkdir -p "$(dirname "$RELEASE_DIR/$relative")"
  cp -p "$SOURCE_ROOT/$relative" "$RELEASE_DIR/$relative"
done

# Linux treats decomposed macOS filenames as different paths from NFC literals.
python3 - "$RELEASE_DIR/assistant_web/scripts/commands" <<'PY'
import sys
import unicodedata
from pathlib import Path

for path in Path(sys.argv[1]).glob("*.command"):
    target = path.with_name(unicodedata.normalize("NFC", path.name))
    if path.name == target.name:
        continue
    if target.exists() and not path.samefile(target):
        raise SystemExit(f"Command filename normalization collision: {path.name}")
    path.rename(target)
PY
