# Real-call tests

How to write up a test run on a real call: a meeting folder Bryce recorded with the app (usually on the target laptop) and hands over, often as a download.

A real call is **private**: people's names, the meeting title, the team's terms, bookmark labels and what was said belong to other people. They stay on this machine. Everything that reaches git or GitHub is anonymised.

## Steps

1. **Number the test.** Run `git pull`, then take the highest `## From Test #N` in `TODO.md` plus one. Another session may be writing up a different call at the same time, so number it right before writing, not at the start.

2. **Make it private first.** Add every identifier in the folder to `lab/private_terms.txt` (format in `tools/check_private.py`):
   - the title and attendees in `meta.json`
   - the names in `speakers.json` and `speaker_tags.jsonl`
   - the `vocabulary` terms that name people, the team, products or the company
   - the bookmark labels in `marks.jsonl`

   Leave out only common words that would match everywhere (such as "Will"). Bryce's own name is already public in the repo. Done when every identifier is either in the file or deliberately left out.

3. **Copy the folder** into `lab/src/` (not in git) and work only on the copy. In writing, refer to it by date and time ("the 2026-11-11 08:33 folder"), not by its folder ID: the ID contains the title.

4. **Ask Bryce for the ground truth** the files can't give:
   - which machine recorded it
   - how many people spoke
   - what he did during the call (named speakers or lines, bookmarks, vocabulary, mutes, clips), and anything done on purpose as a test

5. **Measure.** Compare with the last test on the same machine, and with Tests #4–#7 in `TODO.md`.
   - **Capture**: `clean_exit`, `DONE`, `gaps` (mutes and anything else), `echo_segments_dropped`, `devices`.
   - **Live view**: `live_stats` (delay median, p90 and max, backlog, lag).
   - **Final pass**: `final_stats` (`took_s`, speed as × meeting length, stages).
   - **Speakers**: for each live label, how many seconds land on each final speaker. Count the final speakers against Bryce's count, and check that every named line kept its name.
   - **Features used**: bookmarks (`marks.jsonl`), vocabulary (`vocabulary`, `vocabulary_changed_at_ms`), clips played (`playbacks`).
   - **Transcript**: the share of lines under 1 s, and of lines with low confidence.

   Done when every feature Bryce used has a result, or a reason it could not be checked (the app log stays on the laptop, for example).

6. **Record it in `TODO.md`** as `## From Test #N: <setup in a few words> (<date>)`:
   - A setup paragraph: machine, devices, model, Keep audio, length, line counts, how many remote people, what Bryce did.
   - `### Worked`: one `- [x]` item per result.
   - `### Open`: one `- [ ]` item per problem, each with the issue that tracks it.

   People are "Remote A", "Remote B"… by final talk time, largest first, and the mic side is "the laptop's user". Paraphrase what was said instead of quoting it. Replace team terms with a generic description ("a product name").

7. **Post to GitHub.** Write each body to a file in the scratchpad, then send it with `--body-file`:
   - Comment the test's results on each open issue it covers (`gh issue list`).
   - File each new finding with `gh issue create --label needs-triage`.

   Done when every `### Open` item names an issue.

8. **Commit** `TODO.md`. The hooks check the diff and the commit message, and the Claude Code hook checks each `gh` body. If one blocks, put a placeholder in place of the term, and add the term to `lab/private_terms.txt` if it was missing.
