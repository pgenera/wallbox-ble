"""Unit tests for the pure-Python BAPI protocol layer."""

from __future__ import annotations

import json

import importlib.util
import pathlib
import pytest

# Load bapi.py directly so we don't trigger the package __init__ (which imports
# homeassistant). bapi.py is pure-stdlib and has no other deps.
_BAPI_PATH = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "wallbox_ble" / "bapi.py"
import sys
_spec = importlib.util.spec_from_file_location("wallbox_ble_bapi", _BAPI_PATH)
bapi = importlib.util.module_from_spec(_spec)
sys.modules["wallbox_ble_bapi"] = bapi
_spec.loader.exec_module(bapi)


# ---------------------------------------------------------------------------
# frame / build_cmd
# ---------------------------------------------------------------------------

def _hand_frame(payload: bytes) -> bytes:
    body = b"EaE" + bytes([len(payload)]) + payload
    return body + bytes([sum(body) & 0xFF])


def test_frame_short_payload_matches_reference():
    payload = b'{"met":"r_dat","par":null,"id":1}'
    assert bapi.frame(payload) == _hand_frame(payload)


def test_frame_checksum_is_sum_mod_256():
    payload = b"x" * 10
    framed = bapi.frame(payload)
    body, cs = framed[:-1], framed[-1]
    assert cs == sum(body) & 0xFF


def test_frame_long_payload_uses_ascii_length():
    payload = b"a" * 300
    framed = bapi.frame(payload)
    assert framed.startswith(b"EaE\x00300\x00")
    assert framed[-1] == sum(framed[:-1]) & 0xFF
    # body should still contain the payload intact
    assert payload in framed


def test_frame_payload_255_uses_one_byte_length():
    payload = b"a" * 255
    framed = bapi.frame(payload)
    assert framed[3] == 255
    assert framed[4:4 + 255] == payload


def test_build_cmd_r_dat_null_par():
    out = bapi.build_cmd("r_dat", None, 1)
    expected_payload = b'{"met":"r_dat","par":null,"id":1}'
    assert out == _hand_frame(expected_payload)


def test_build_cmd_integer_par():
    out = bapi.build_cmd("w_lck", 1, 7)
    expected = b'{"met":"w_lck","par":1,"id":7}'
    assert out == _hand_frame(expected)


def test_build_cmd_dict_par_is_compact():
    out = bapi.build_cmd("s_alo", {"enabled": 1, "time": 60}, 3)
    # No whitespace; key order preserved from dict insertion.
    expected = b'{"met":"s_alo","par":{"enabled":1,"time":60},"id":3}'
    assert out == _hand_frame(expected)


def test_build_cmd_string_par_quoted():
    out = bapi.build_cmd("s_tzn", "Europe/Madrid", 2)
    expected = b'{"met":"s_tzn","par":"Europe/Madrid","id":2}'
    assert out == _hand_frame(expected)


# ---------------------------------------------------------------------------
# ResponseParser
# ---------------------------------------------------------------------------

def test_parser_single_chunk_object():
    p = bapi.ResponseParser()
    out = p.feed(b'{"id":1,"r":{"st":2}}')
    assert out == [{"id": 1, "r": {"st": 2}}]


def test_parser_split_across_byte_chunks():
    p = bapi.ResponseParser()
    data = b'{"id":42,"r":{"cp":3.2,"L1":160}}'
    collected: list[dict] = []
    for byte in data:
        collected.extend(p.feed(bytes([byte])))
    assert collected == [{"id": 42, "r": {"cp": 3.2, "L1": 160}}]


def test_parser_handles_braces_inside_strings():
    p = bapi.ResponseParser()
    msg = b'{"id":5,"r":{"name":"weird}name{ok"}}'
    out = p.feed(msg)
    assert out == [{"id": 5, "r": {"name": "weird}name{ok"}}]


def test_parser_handles_escaped_quotes():
    p = bapi.ResponseParser()
    msg = b'{"id":6,"r":{"v":"a\\"b"}}'
    out = p.feed(msg)
    assert out == [{"id": 6, "r": {"v": 'a"b'}}]


def test_parser_skips_leading_garbage():
    p = bapi.ResponseParser()
    out = p.feed(b'\x00\x00garbage{"id":9,"r":1}')
    assert out == [{"id": 9, "r": 1}]


def test_parser_two_back_to_back_objects():
    p = bapi.ResponseParser()
    out = p.feed(b'{"id":1,"r":1}{"id":2,"r":2}')
    assert out == [{"id": 1, "r": 1}, {"id": 2, "r": 2}]


# ---------------------------------------------------------------------------
# Status and constants
# ---------------------------------------------------------------------------

def test_status_name_known_codes():
    # Default = Pulsar MAX scheme
    assert bapi.status_name(2) == "Charging"
    assert bapi.status_name(161) == "Ready"
    assert bapi.status_name(194) == "Locked"


def test_status_name_pulsar_plus_differs():
    # Same code, different firmware -> different meaning.
    assert bapi.status_name(1, "pulsar_plus") == "Charging"
    assert bapi.status_name(1, "pulsar_max") == "Connected"
    assert bapi.status_name(6, "pulsar_plus") == "Locked"
    assert bapi.status_name(6, "pulsar_max") == "Error"


def test_is_charging_layout_aware():
    assert bapi.is_charging(1, "pulsar_plus") is True
    assert bapi.is_charging(1, "pulsar_max") is False
    assert bapi.is_charging(2, "pulsar_max") is True
    assert bapi.is_charging(2, "pulsar_plus") is False


def test_status_name_unknown_includes_code():
    assert "999" in bapi.status_name(999)
    assert bapi.status_name(None) == "Unknown"


def test_charging_codes_disjoint_from_paused():
    assert bapi.CHARGING_CODES.isdisjoint(bapi.PAUSED_CODES)
    assert bapi.CHARGING_CODES_PULSAR_PLUS.isdisjoint(bapi.PAUSED_CODES_PULSAR_PLUS)


def test_eco_mode_labels_cover_all():
    assert set(bapi.ECO_MODE_LABELS) == {0, 1, 2}
    assert "Full Green" in bapi.ECO_MODE_LABELS[1]
    assert "Eco Smart" in bapi.ECO_MODE_LABELS[2]


def test_known_layouts_distinct():
    uuids = {l.service for l in bapi.KNOWN_LAYOUTS}
    assert len(uuids) == len(bapi.KNOWN_LAYOUTS)
