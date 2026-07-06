"""Shared dataclasses for the summarizer.

Kept in one place so vexa / obsidian / llm / __main__ don't import each other just for types
(no circular deps). Meeting.start/end are datetimes (vexa.py converts the API's epoch floats).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Meeting:
    id: int
    platform: str
    native_meeting_id: str
    start: datetime
    end: datetime


@dataclass
class Utterance:
    speaker: str
    start_time: float
    end_time: float
    text: str


@dataclass
class MeetingMeta:
    participants: list[str]
    date: str  # YYYY-MM-DD (from meeting.start)
    duration: str  # HH:MM:SS
    platform: str
    meeting_id: int
    native_meeting_id: str
