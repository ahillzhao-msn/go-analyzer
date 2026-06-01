#!/bin/bash
set -e
PROJ="/mnt/c/users/ahill/documents/python/go_analysis_project"
cd "$PROJ"

echo "=== Removing old venv ==="
rm -rf .venv_old .venv 2>/dev/null

echo "=== Creating new venv ==="
python3 -m venv .venv
source .venv/bin/activate

echo "=== Installing minimal packages ==="
pip install --quiet --upgrade pip
pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
pip install --quiet numpy pandas scikit-learn tqdm

echo "=== Installing project ==="
pip install -e .

echo "=== Done ==="
du -sh .venv/
pip list --format=columns 2>/dev/null | wc -l
