"""#44: in a final answer of the context command, each line carries the live wording it replaced.

Tested at the context command's seam, ``context.answer(meetings_dir, ...)``. Live and final are matched
by time on the same stream, never by ``seq``. All lines are made up."""

from huddle_helper import context

from .helpers import make_meeting

S = 1000


def done_meeting(tmp_path, live, final):
    return make_meeting(tmp_path, live, final=final)


def final_lines(tmp_path, live, final, **kw):
    done_meeting(tmp_path, live, final)
    ans = context.answer(tmp_path, whole=True, **kw)
    assert ans["ok"] is True, ans
    return ans["lines"]


def test_a_number_the_final_pass_changed_carries_the_live_wording_and_a_number_flag(tmp_path):
    live = [("system", 10 * S, 13 * S, "SPEAKER_01", "We shipped our two items today.")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "We shipped 32 items today.")]

    [line] = final_lines(tmp_path, live, final)

    assert line["text"] == "We shipped 32 items today."
    assert line["live_wording"] == {"text": "We shipped our two items today.", "start_ms": 10 * S,
                                    "end_ms": 13 * S, "flags": ["number"]}


def test_the_same_words_with_other_case_or_punctuation_carry_no_live_wording(tmp_path):
    live = [("system", 10 * S, 13 * S, "SPEAKER_01", "okay so the budget is fine")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "Okay, so the budget is fine.")]

    [line] = final_lines(tmp_path, live, final)

    assert line["live_wording"] is None


def test_a_negation_the_final_pass_added_is_flagged(tmp_path):
    live = [("system", 10 * S, 13 * S, "SPEAKER_01", "We can ship it this week.")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "We can't ship it this week.")]

    [line] = final_lines(tmp_path, live, final)

    assert line["live_wording"]["text"] == "We can ship it this week."
    assert line["live_wording"]["flags"] == ["negation"]


def test_a_changed_capitalised_word_inside_a_sentence_is_flagged_name_like_but_one_starting_it_is_not(tmp_path):
    live = [("system", 10 * S, 13 * S, "SPEAKER_01", "Send the draft to the hub team."),
            ("system", 20 * S, 23 * S, "SPEAKER_01", "So we wait for it.")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "Send the draft to the Zorvex team."),
             ("system", 20 * S, 23 * S, "SPEAKER_01", "Then we wait for it.")]

    named, starts = final_lines(tmp_path, live, final)

    assert named["live_wording"]["flags"] == ["name"]
    assert starts["live_wording"] == {"text": "So we wait for it.", "start_ms": 20 * S, "end_ms": 23 * S,
                                      "flags": []}


def test_changed_days_and_clock_times_are_flagged_date_time(tmp_path):
    live = [("system", 10 * S, 13 * S, "SPEAKER_01", "Let's meet on Tuesday morning."),
            ("system", 20 * S, 23 * S, "SPEAKER_01", "The call is at 3:15.")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "Let's meet on Thursday morning."),
             ("system", 20 * S, 23 * S, "SPEAKER_01", "The call is at 3:50.")]

    day, clock = final_lines(tmp_path, live, final)

    assert day["live_wording"]["flags"] == ["date_time"]
    assert clock["live_wording"]["flags"] == ["number", "date_time"]


def test_live_lines_are_matched_by_time_on_the_same_stream_not_by_seq(tmp_path):
    # The final line is seq 1; live seq 1 is an unrelated earlier line, and the mic said other words then.
    live = [("system", 1 * S, 3 * S, "SPEAKER_01", "Hello everyone."),
            ("mic", 10 * S, 13 * S, "You", "Sounds good to me."),
            ("system", 10 * S, 13 * S, "SPEAKER_01", "The order was nine boxes.")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "The order was five boxes.")]

    [line] = final_lines(tmp_path, live, final)

    assert line["live_wording"]["text"] == "The order was nine boxes."
    assert line["live_wording"]["flags"] == ["number"]


def test_a_final_line_with_no_live_line_at_its_time_carries_no_live_wording(tmp_path):
    live = [("system", 1 * S, 3 * S, "SPEAKER_01", "Hello everyone.")]
    final = [("system", 1 * S, 3 * S, "SPEAKER_01", "Hello everyone."),
             ("system", 30 * S, 33 * S, "SPEAKER_01", "Something live missed.")]

    assert [line["live_wording"] for line in final_lines(tmp_path, live, final)] == [None, None]


def test_one_live_line_split_into_two_final_lines_differs_only_where_the_words_differ(tmp_path):
    live = [("system", 10 * S, 16 * S, "SPEAKER_01", "We agreed on the plan. Then we left at four.")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "We agreed on the plan."),
             ("system", 13 * S, 16 * S, "SPEAKER_01", "Then we left at five.")]

    first, second = final_lines(tmp_path, live, final)

    assert first["live_wording"] is None
    # only the live words that line up with this final line; the times are those of the live line
    assert second["live_wording"] == {"text": "Then we left at four.", "start_ms": 10 * S, "end_ms": 16 * S,
                                      "flags": ["number"]}


def test_a_live_answer_is_unchanged(tmp_path):
    live = [("system", 10 * S, 13 * S, "SPEAKER_01", "We shipped our two items today.")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "We shipped 32 items today.")]

    [line] = final_lines(tmp_path, live, final, transcript="live")

    assert line["text"] == "We shipped our two items today."
    assert "live_wording" not in line


def test_an_unreadable_live_file_is_a_warning_and_no_live_wording(tmp_path):
    line = ("system", 10 * S, 13 * S, "SPEAKER_01", "We shipped 32 items today.")
    folder = done_meeting(tmp_path, [line], [line])
    folder.live_path.unlink()
    folder.live_path.mkdir()  # exists, cannot be opened as a file

    ans = context.answer(tmp_path, whole=True)

    assert ans["ok"] is True
    assert [l["live_wording"] for l in ans["lines"]] == [None]
    assert [w["file"].endswith("transcript.jsonl") for w in ans["warnings"]] == [True]


def test_a_word_dropped_at_the_start_is_kept_when_the_live_line_starts_only_a_little_earlier(tmp_path):
    live = [("system", 9950, 13 * S, "SPEAKER_01", "No, we ship it this week.")]
    final = [("system", 10 * S, 13 * S, "SPEAKER_01", "We ship it this week.")]

    [line] = final_lines(tmp_path, live, final)

    assert line["live_wording"]["text"] == "No, we ship it this week."
    assert line["live_wording"]["flags"] == ["negation"]


def test_clock_times_with_am_or_pm_are_date_time_and_a_new_live_line_starts_a_sentence(tmp_path):
    live = [("system", 10 * S, 12 * S, "SPEAKER_01", "so the call is"),
            ("system", 12 * S, 14 * S, "SPEAKER_01", "Then at 3pm")]
    final = [("system", 10 * S, 14 * S, "SPEAKER_01", "So the call is later at 5pm")]

    [line] = final_lines(tmp_path, live, final)

    assert line["live_wording"]["flags"] == ["number", "date_time"]
