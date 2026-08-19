#!/bin/bash
cd "$(dirname "$(readlink -f "$0")")"
PYTHON_BIN="${AUTORESEARCH_PYTHON:-python3}"
"$PYTHON_BIN" -u idea_generation.py >> logs/cron.log 2>&1
