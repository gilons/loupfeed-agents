"""Tests for the Teams inbound gate.

With RSC (``ChannelMessage.Read.Group``) Teams delivers every channel message
to the bot, so the adapter must decide for itself what is addressed to it.
"""

from __future__ import annotations

import pytest

from agent import teams_adapter

BOT_APP_ID = "80a79e55-421e-469d-a3bd-9a36f07803ea"


def _activity(*, conv_type: str, conv_id: str, entities=None, activity_id: str = "m1") -> dict:
    return {
        "type": "message",
        "id": activity_id,
        "text": "what did we decide?",
        "conversation": {"id": conv_id, "conversationType": conv_type},
        "recipient": {"id": f"28:{BOT_APP_ID}"},
        "entities": entities or [],
    }


def _mention(mentioned_id: str) -> dict:
    return {"type": "mention", "mentioned": {"id": mentioned_id, "name": "loupfeed"}}


@pytest.fixture(autouse=True)
def _app_id(monkeypatch):
    monkeypatch.setenv("TEAMS_APP_ID", BOT_APP_ID)


@pytest.fixture
def _no_sessions(monkeypatch):
    """No LangGraph thread exists for any key (never engaged before)."""

    async def _never(_thread_id: str) -> bool:
        return False

    monkeypatch.setattr(teams_adapter, "_thread_exists", _never)


@pytest.fixture
def _engaged(monkeypatch):
    """A LangGraph thread already exists — we are in this conversation."""

    async def _always(_thread_id: str) -> bool:
        return True

    monkeypatch.setattr(teams_adapter, "_thread_exists", _always)


def test_mention_matches_channel_prefixed_id():
    activity = _activity(
        conv_type="channel",
        conv_id="19:chan@thread.tacv2",
        entities=[_mention(f"28:{BOT_APP_ID}")],
    )
    assert teams_adapter._mentions_bot(activity) is True


def test_mention_matches_bare_app_id():
    activity = _activity(
        conv_type="channel", conv_id="19:chan@thread.tacv2", entities=[_mention(BOT_APP_ID)]
    )
    assert teams_adapter._mentions_bot(activity) is True


def test_mention_of_someone_else_is_not_us():
    activity = _activity(
        conv_type="channel",
        conv_id="19:chan@thread.tacv2",
        entities=[_mention("29:some-human-aad-id")],
    )
    assert teams_adapter._mentions_bot(activity) is False


@pytest.mark.asyncio
async def test_channel_chatter_is_ignored(_no_sessions):
    """The regression this gate exists for: plain team talk must not trigger us."""
    activity = _activity(conv_type="channel", conv_id="19:chan@thread.tacv2")
    assert await teams_adapter._is_addressed_to_us(activity) is False


@pytest.mark.asyncio
async def test_channel_mention_is_handled(_no_sessions):
    activity = _activity(
        conv_type="channel",
        conv_id="19:chan@thread.tacv2",
        entities=[_mention(f"28:{BOT_APP_ID}")],
    )
    assert await teams_adapter._is_addressed_to_us(activity) is True


@pytest.mark.asyncio
async def test_personal_chat_needs_no_mention(_no_sessions):
    activity = _activity(conv_type="personal", conv_id="a:1:1-with-giles")
    assert await teams_adapter._is_addressed_to_us(activity) is True


@pytest.mark.asyncio
async def test_followup_in_engaged_thread_needs_no_mention(_engaged):
    """Once we're in a thread, replies continue the conversation untagged."""
    activity = _activity(conv_type="channel", conv_id="19:chan@thread.tacv2;messageid=1700")
    assert await teams_adapter._is_addressed_to_us(activity) is True


@pytest.mark.asyncio
async def test_meeting_chatter_is_ignored(_no_sessions):
    activity = _activity(conv_type="groupChat", conv_id="19:meeting_abc@thread.v2")
    assert await teams_adapter._is_addressed_to_us(activity) is False
