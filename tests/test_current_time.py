"""The model must be told what day it is, freshly, on every call.

Without a timestamp the model resolves "today" from dates in tool results,
which is how Friday's standup recording (31 July) was summarised as "Today's
Standup" on Tuesday 4 August.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.middleware.current_time import current_time_stamp


def test_stamp_carries_the_current_berlin_date():
    stamp = current_time_stamp()
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    assert now.strftime("%d %B %Y") in stamp
    assert now.strftime("%A") in stamp
    assert "Europe/Berlin" in stamp


def test_stamp_instructs_relative_date_resolution():
    stamp = current_time_stamp()
    assert "today" in stamp
    assert "never against dates found in tool results" in stamp


def test_stamp_has_hour_precision():
    assert re.search(r"\b\d{2}:\d{2}\b", current_time_stamp())


def test_timezone_can_be_overridden():
    stamp = current_time_stamp("UTC")
    assert "UTC" in stamp
