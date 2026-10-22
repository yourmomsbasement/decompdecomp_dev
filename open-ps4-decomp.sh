#!/bin/sh
# decompdecomp — the PlayStation 4 fork (git checkout at this directory).
# A checkout with .git is the DEV edition: its own home (~/Decomp-dev), its own port (6474), its own
# window class (decompdecomp-dev), so it never touches the released app in ~/Decomp on port 6464.
# The PS4 projects build with /opt/llvm-5.0.2-globalguard/bin/clang (global stack guard); see FORK-PS4.md.
cd "$(dirname "$0")" || exit 1
exec ./.venv/bin/python -m decompnet.app.cli "$@"
