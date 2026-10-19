#!/bin/zsh

git config --global --add safe.directory "*"

uv sync --quiet

# activate venv
source .venv/bin/activate

# setup pre-commit hook
pre-commit install
