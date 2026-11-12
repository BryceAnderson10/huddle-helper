# Sourced by the hooks: sets $root (this checkout) and $py (a Python with the dev tools).
# A git worktree has no .venv of its own, so fall back to the main checkout's.
root=$(git rev-parse --show-toplevel)
main=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
py=python
for dir in "$root" "$main"; do
  for exe in "$dir/.venv/Scripts/python.exe" "$dir/.venv/bin/python"; do
    if [ -x "$exe" ]; then py=$exe; break 2; fi
  done
done
