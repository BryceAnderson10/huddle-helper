# A fresh heartbeat means a process holds the meeting

Recover closes out meetings whose recording did not end with Stop, and it must never touch a meeting another process is still working on. Until now the heartbeat only meant "recording", so a meeting that had stopped and was waiting for, or running, its final pass in another process looked the same to recover as one left by a crash. We decided that a fresh heartbeat means a process holds the meeting: it is recording it, running its final pass, or holding it in its final pass queue. Recover skips any meeting with a fresh heartbeat, whatever its status. **Active meeting** keeps its definition: status `recording` and a fresh heartbeat. A meeting with status `recording` and a stale or missing heartbeat is still shown as **Recorder died**. The code and the heartbeat section of `docs/OUTPUT_SPEC.md` still describe the narrower meaning; the work that widens the heartbeat updates both.

## Considered Options

- **A lock file per meeting.** Rejected: it is one more file to clean up after a crash, and a stale lock needs the same freshness rule as the heartbeat anyway.
- **Recover asking the running tray what it holds.** Rejected: it needs a channel between processes, which ADR 0001 avoided by making the meeting files the contract.
