# The meeting files are the contract; a read-only command is the supported way in

A downstream app needs current, attributable meeting context: which meeting is active, a bounded window of what was said, who said it, and whether that text is live or final and which version it is. We decided that the meeting folder and its documented files (`docs/OUTPUT_SPEC.md`) stay the official contract, and that Huddle Helper adds one read-only command that returns such a window as JSON, built on the same reader the Transcript window uses. Huddle Helper does not keep earlier final transcripts: re-running the final pass replaces the file and changes its transcript version, and a downstream app that needs to prove what it quoted saves the quote itself.

## Considered Options

- **A server inside Huddle Helper (MCP or a local web API).** Rejected: it would run all the time, needs security decisions, and is one more thing that can break on a single-user laptop. A downstream app can wrap the command as a tool itself.
- **A Python library.** Rejected: it only serves consumers written in Python, and it ties them to Huddle Helper's internals.
- **Making the command, not the files, the contract.** Rejected: every consumer would then need Huddle Helper installed and runnable, and the file spec would stop being maintained.
- **Keeping every earlier final transcript.** Rejected: it adds disk use, deletion rules, and keeps meeting text around longer than the user chose. Keeping evidence for its own answers is the downstream app's job.
