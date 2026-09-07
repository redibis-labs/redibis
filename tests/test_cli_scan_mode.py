"""Tests for CLI scan mode parsing."""

from __future__ import annotations

import pytest

from redibis.scan_mode import parse_scan_mode


def test_parse_all():
    assert parse_scan_mode("all") == (True, True, True)


def test_parse_comma_list():
    assert parse_scan_mode("pii,quality") == (True, True, True)


def test_parse_profile_only():
    assert parse_scan_mode("profile") == (False, True, False)


def test_parse_unknown_raises():
    with pytest.raises(ValueError, match="unknown"):
        parse_scan_mode("bogus")
