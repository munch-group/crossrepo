#!/usr/bin/env bash
# Fail the task when any step fails. Without this the script's status is that of
# the final `cd`, so a broken docstring or a render error reported success.
set -euo pipefail

python scripts/docs-cli-reference.py     # the CLI page, read out of click
cd docs
#rm -f api/_styles-quartodoc.css api/_sidebar.yml #*.qmd
quartodoc build
quartodoc interlinks
quarto render
cd ..
