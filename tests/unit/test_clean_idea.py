from __future__ import annotations

import pytest

from superagent.creative.story import StoryEngine, brief_from_command, clean_idea


@pytest.mark.parametrize(
    ("command", "subject"),
    [
        ("chai wale ki kahani, 20 second, hinglish (make a video)", "chai wale ki kahani"),
        ("create a 30 second video about a lonely robot who learns to paint in English", "a lonely robot who learns to paint"),
        ("ek chhote bachche ki kahani jo gaon se shehar aaya, 45 sec, 9:16 video banao", "ek chhote bachche ki kahani jo gaon se shehar aaya"),
        ("चाय वाले की कहानी 20 सेकंड हिंदी में", "चाय वाले की कहानी"),
    ],
)
def test_instructions_are_removed_but_the_subject_stays(command: str, subject: str):
    assert clean_idea(command) == subject


def test_a_bare_instruction_is_kept_rather_than_emptied():
    assert clean_idea("make a video") == "make a video"


def test_template_story_follows_the_subject_not_the_instructions(settings):
    brief = brief_from_command("chai wale ki kahani, 20 second, hinglish (make a video)", {"duration_seconds": 20}, "hinglish")
    story = StoryEngine(None).template(brief, __import__("superagent.creative.models", fromlist=["StyleBible"]).StyleBible())
    text = " ".join([story.title, story.synopsis, *(s.action + " " + (s.narration or "") for s in story.scenes)])
    assert "chai wale ki kahani" in text
    for leaked in ("20 second", "make a video", "(make"):
        assert leaked not in text
