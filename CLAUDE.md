## Tests

Run them with the venv's Python: `.venv\Scripts\python -m pytest` (`-m "not models"` skips the real-model test). The system `python` has no pytest.

## Agent skills

### Issue tracker

Issues live in GitHub Issues for BryceAnderson10/huddle-helper (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Real-call tests and private terms

A meeting folder from a real call → follow `docs/agents/real-call-tests.md`. Names, titles and team terms from real calls stay out of git and GitHub; `tools/check_private.py` blocks them (list in the gitignored `lab/private_terms.txt`) from `.githooks/` (per clone: `git config core.hooksPath .githooks`) and a PreToolUse hook on `gh` commands.
