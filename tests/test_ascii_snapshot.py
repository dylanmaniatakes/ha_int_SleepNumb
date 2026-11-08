"""Tests for the ASCII-plane snapshot mapping and presence helper (no HA needed).

Replays the REAL batch responses captured on Dylan's bed (btsleep captures in
the RE workspace) through snapshot_from_batch and presence_from_pressure.
Run: .venv/bin/python -m pytest tests/test_ascii_snapshot.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bamkey_ascii",
    Path(__file__).parent.parent / "custom_components" / "sleepnumber_pro" / "bamkey_ascii.py",
)
ascii_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["bamkey_ascii"] = ascii_mod
_SPEC.loader.exec_module(ascii_mod)


def snap_of(raw_values: list[str]):
    return ascii_mod.snapshot_from_batch(
        [v[5:] if v.startswith("PASS:") else v for v in raw_values]
    )


class TestSnapshotFromBatch:
    def test_live_response_full(self):
        """The exact 15-value batch Dylan's bed answered on 2026-11-08."""
        values = ["low 0", "0 55 60", "35", "16", "0", "none 0", "snore",
                  "off 0 0", "0 40 50", "35", "16", "0", "none 0", "none"]
        snap = ascii_mod.snapshot_from_batch(values)
        assert snap.light == "low 0"
        assert snap.sn_left.sleep_number == 55
        assert snap.sn_left.pressure == 60
        assert snap.sn_left.adjusting is False
        assert snap.fav_left == 35
        assert snap.sn_right.sleep_number == 40
        assert snap.sn_right.pressure == 50
        assert snap.fav_right == 35
        assert snap.pos_head == 16
        assert snap.pos_foot == 0
        assert snap.preset == "snore"
        assert snap.light_auto == "off 0 0"
        assert snap.error is None

    def test_short_batch_tolerated(self):
        snap = ascii_mod.snapshot_from_batch(["low 0", "0 70 50", "35"])
        assert snap.light == "low 0"
        assert snap.sn_left.sleep_number == 70
        assert snap.fav_right is None

    def test_fail_keeps_parsing(self):
        values = ["low 0", "FAIL:20", "35", "16", "0", "none 0", "snore", "off 0 0"]
        snap = snap_of(values)
        assert snap.error == "FAIL:20"
        assert snap.light == "low 0"
        assert snap.sn_left is None
        assert snap.fav_left == 35
        assert snap.pos_head == 16

    def test_adjusting_state_preserved(self):
        values = ["low 0", "1 40 50", "35", "0", "0"]
        snap = snap_of(values)
        assert snap.sn_left.adjusting is True
        assert snap.sn_left.sleep_number == 40

    def test_empty_batch_records_error(self):
        snap = ascii_mod.snapshot_from_batch([])
        assert snap.error == "empty batch response"


class TestPresenceFromPressure:
    def test_above_baseline_occupied(self):
        assert ascii_mod.presence_from_pressure(70, 50) is True
        assert ascii_mod.presence_from_pressure(57, 50) is True

    def test_at_or_below_baseline_empty(self):
        assert ascii_mod.presence_from_pressure(50, 50) is False
        assert ascii_mod.presence_from_pressure(55, 50) is False  # exactly delta

    def test_unknown_inputs(self):
        assert ascii_mod.presence_from_pressure(None, 50) is None
        assert ascii_mod.presence_from_pressure(60, None) is None


class TestFramingRoundTrip:
    def test_build_matches_wire(self):
        assert ascii_mod.build_ascii_frame("PSNS right 40").hex().upper() == (
            "66557A496F4E1B00000050534E53207269676874203430C91D2AA6"
        )

    def test_parse_live_response_frames(self):
        # The bed's read-back response frame for PASS:<light state>, wire-captured
        # and CRC-verified during the 2026-11-08 session.
        frame = ascii_mod.build_ascii_frame("PASS:low 0")
        assert ascii_mod.parse_ascii_frames(frame) == ["PASS:low 0"]
        assert ascii_mod.parse_response("PASS:low 0") == "low 0"
