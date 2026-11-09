"""The sitting plan is each member's material, and the closing talk uses only that."""

from mockboard.brain.agenda import build_fallback_agenda
from mockboard.brain.board_brief import brief_from, discussion_lines
from mockboard.brain.schemas import PanelMemory, TurnNote


def test_the_plan_is_ready_before_anyone_has_spoken(form):
    agenda = build_fallback_agenda(form)
    packets = brief_from(agenda, PanelMemory(), {"chairman": "Chairperson Sharma"})
    assert len(packets) == 5
    chair = packets[0]
    assert chair.name == "Chairperson Sharma"
    assert chair.questions and chair.ahead == chair.questions and chair.heard == ()


def test_a_heard_note_moves_that_seed_out_of_what_is_ahead(form):
    agenda = build_fallback_agenda(form)
    memory = PanelMemory(
        asked_seeds={"form_hr": [0]},
        notes=[TurnNote(
            segment_id="form_hr", member="chairman", question="Why this service?",
            gist="The candidate wants to serve people in the district.",
        )],
    )
    packets = brief_from(agenda, memory, {})
    chair = packets[0]
    assert chair.heard == ("The candidate wants to serve people in the district.",)
    assert chair.questions[0] not in chair.ahead
    assert len(chair.ahead) == len(chair.questions) - 1


def test_the_discussion_quotes_notes_and_does_not_invent_a_figure(form):
    agenda = build_fallback_agenda(form)
    memory = PanelMemory(notes=[TurnNote(
        segment_id="degree", member="economist", question="What did study teach you?",
        gist="Electronics taught the candidate to think in systems.",
    )])
    lines = discussion_lines(
        agenda, memory, {"economist": "Doctor Iyer"}, "What would you do first?",
    )
    text = "\n".join(lines)
    heard = "Doctor Iyer: I heard this. Electronics taught the candidate to think in systems."
    assert heard in text
    assert "What would you do first?" in text
    assert "percent" not in text and not any(ch.isdigit() for ch in text)
