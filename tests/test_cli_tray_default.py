"""No subcommand (HuddleHelper.exe double-clicked) must run the tray with the tray's own defaults."""

from huddle_helper import cli, tray


def test_no_subcommand_runs_the_tray(monkeypatch):
    calls = []
    monkeypatch.setattr(tray, "run_tray", lambda *a, **kw: calls.append((a, kw)) or 0)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
    assert cli.main([]) == 0 and cli.main(["-v"]) == 0 and cli.main(["tray"]) == 0
    assert len(calls) == 3 and calls[0] == calls[2]
    assert calls[0] == ((None, None), {"auto_open_window": True, "open_latest": False, "open_meetings": False})
