#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if ! command -v jpegtran >/dev/null 2>&1 && [ ! -x /opt/homebrew/opt/jpeg-turbo/bin/jpegtran ] && [ ! -x /usr/local/opt/jpeg-turbo/bin/jpegtran ]; then
  echo ""
  echo "NOTE: CPCe-safe lossless JPEG optimization requires jpegtran from libjpeg-turbo."
  echo "Install it with Homebrew: brew install jpeg-turbo"
  echo "Validation and package preparation still work without it."
  echo ""
fi
python -m uvicorn app:app --reload
