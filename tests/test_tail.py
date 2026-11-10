import json

from huddle_helper.tail import JsonlTail


def line(n) -> bytes:
    return (json.dumps({"n": n}) + "\n").encode()


def test_missing_file_returns_nothing_then_reads_from_start(tmp_path):
    path = tmp_path / "t.jsonl"
    tail = JsonlTail(path)
    poll = tail.poll()
    assert poll.events == [] and not poll.reset and not poll
    path.write_bytes(line(1) + line(2))
    assert [e["n"] for e in tail.poll()] == [1, 2]
    assert tail.poll().events == []


def test_appended_lines_come_once(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(line(1))
    tail = JsonlTail(path)
    assert [e["n"] for e in tail.poll()] == [1]
    with open(path, "ab") as fh:
        fh.write(line(2) + line(3))
    assert [e["n"] for e in tail.poll()] == [2, 3]
    assert tail.poll().events == []


def test_partial_last_line_is_held_back_until_complete(tmp_path):
    path = tmp_path / "t.jsonl"
    whole = line(2)
    path.write_bytes(line(1) + whole[:5])
    tail = JsonlTail(path)
    assert [e["n"] for e in tail.poll()] == [1]
    with open(path, "ab") as fh:
        fh.write(whole[5:-1])
    assert tail.poll().events == []  # still no newline
    with open(path, "ab") as fh:
        fh.write(whole[-1:])
    assert [e["n"] for e in tail.poll()] == [2]


def test_multibyte_text_split_across_reads(tmp_path):
    path = tmp_path / "t.jsonl"
    raw = (json.dumps({"t": "café – ok"}, ensure_ascii=False) + "\n").encode("utf-8")
    cut = raw.index("é".encode()) + 1  # in the middle of the two-byte character
    path.write_bytes(raw[:cut])
    tail = JsonlTail(path)
    assert tail.poll().events == []
    with open(path, "ab") as fh:
        fh.write(raw[cut:])
    assert tail.poll().events == [{"t": "café – ok"}]


def test_shrink_resets_and_rereads(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(line(1) + line(2) + line(3))
    tail = JsonlTail(path)
    assert len(tail.poll()) == 3
    path.write_bytes(line(9))  # rewritten shorter
    poll = tail.poll()
    assert poll.reset and [e["n"] for e in poll] == [9]
    assert not tail.poll().reset


def test_replaced_file_of_same_size_resets(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(line(1))
    tail = JsonlTail(path)
    tail.poll()
    other = tmp_path / "new.jsonl"
    other.write_bytes(line(7))
    other.replace(path)  # same size, new file
    poll = tail.poll()
    assert poll.reset and [e["n"] for e in poll] == [7]


def test_vanished_file_signals_reset_once(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(line(1))
    tail = JsonlTail(path)
    tail.poll()
    path.unlink()
    assert tail.poll().reset
    assert not tail.poll().reset
    path.write_bytes(line(5))
    assert [e["n"] for e in tail.poll()] == [5]


def test_bad_lines_are_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(line(1) + b"not json\n[1,2]\n\n" + line(2))
    assert [e["n"] for e in JsonlTail(path).poll()] == [1, 2]


def test_max_events_keeps_the_rest_for_the_next_poll(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"".join(line(i) for i in range(5)))
    tail = JsonlTail(path)
    assert [e["n"] for e in tail.poll(max_events=2)] == [0, 1]
    assert tail.has_more
    assert [e["n"] for e in tail.poll(max_events=2)] == [2, 3]
    assert [e["n"] for e in tail.poll(max_events=2)] == [4]
    assert not tail.has_more
