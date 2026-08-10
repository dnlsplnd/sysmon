"""The shared read helpers and formatters.

The read helpers matter more than they look: every collector depends on them
returning a default rather than raising, because sysfs is full of files that
exist and still fail to read.
"""

from __future__ import annotations

import pytest

from sysmon import util


# ------------------------------------------------------------------ reads


def test_read_text_strips_the_trailing_newline(tmp_path):
    path = tmp_path / "name"
    path.write_text("k10temp\n")
    assert util.read_text(str(path)) == "k10temp"


def test_read_text_returns_the_default_when_missing(tmp_path):
    assert util.read_text(str(tmp_path / "absent")) is None
    assert util.read_text(str(tmp_path / "absent"), "fallback") == "fallback"


def test_read_text_returns_the_default_on_an_unreadable_file(tmp_path):
    """A directory is the cheapest stand-in for the EACCES/EIO files sysfs has."""
    assert util.read_text(str(tmp_path), "fallback") == "fallback"


def test_read_text_survives_undecodable_bytes(tmp_path):
    path = tmp_path / "raw"
    path.write_bytes(b"\xff\xfe ok")
    assert "ok" in util.read_text(str(path))


def test_read_int_takes_the_first_token(tmp_path):
    # /proc/stat's "intr" line is one total followed by hundreds of columns.
    path = tmp_path / "intr"
    path.write_text("123456 1 2 3 4\n")
    assert util.read_int(str(path)) == 123456


def test_read_int_falls_back_when_not_a_number(tmp_path):
    path = tmp_path / "value"
    path.write_text("N/A\n")
    assert util.read_int(str(path)) is None
    assert util.read_int(str(path), -1) == -1


def test_read_int_falls_back_on_an_empty_file(tmp_path):
    path = tmp_path / "empty"
    path.write_text("")
    assert util.read_int(str(path), 7) == 7


def test_read_float_parses_and_falls_back(tmp_path):
    path = tmp_path / "value"
    path.write_text("3.5 extra\n")
    assert util.read_float(str(path)) == 3.5
    assert util.read_float(str(tmp_path / "absent"), 1.5) == 1.5


def test_listdir_is_sorted_and_empty_when_missing(tmp_path):
    (tmp_path / "hwmon2").mkdir()
    (tmp_path / "hwmon0").mkdir()
    (tmp_path / "hwmon1").mkdir()
    assert util.listdir(str(tmp_path)) == ["hwmon0", "hwmon1", "hwmon2"]
    assert util.listdir(str(tmp_path / "absent")) == []


# ------------------------------------------------------------- formatters


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "--"),
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.00 KiB"),
        (1536, "1.50 KiB"),
        (10 * 1024, "10.0 KiB"),
        (11 * 1024**3, "11.0 GiB"),
    ],
)
def test_fmt_bytes(value, expected):
    assert util.fmt_bytes(value) == expected


def test_fmt_bytes_honours_an_explicit_precision():
    assert util.fmt_bytes(1024, precision=0) == "1 KiB"


def test_fmt_bytes_keeps_the_sign():
    assert util.fmt_bytes(-2048) == "-2.00 KiB"


@pytest.mark.parametrize(
    "value, expected",
    [(None, "--"), (0, "0 B/s"), (2048, "2.00 KiB/s"), (12.4 * 1024**2, "12.4 MiB/s")],
)
def test_fmt_rate(value, expected):
    assert util.fmt_rate(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "--"),
        (100, "800 bit/s"),
        # Link rates are marketed in SI units, so this scales by 1000, not 1024.
        (1000, "8.00 kbit/s"),
        (125_000_000, "1.00 Gbit/s"),
    ],
)
def test_fmt_bits_scales_by_1000_not_1024(value, expected):
    assert util.fmt_bits(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(None, "--"), (533, "533 MHz"), (999, "999 MHz"), (1000, "1.00 GHz"), (3700, "3.70 GHz")],
)
def test_fmt_hz_promotes_at_a_thousand(value, expected):
    assert util.fmt_hz(value) == expected


@pytest.mark.parametrize(
    "value, expected", [(None, "--"), (20.94, "20.9 W"), (99.9, "99.9 W"), (150.0, "150 W")]
)
def test_fmt_watts_drops_the_decimal_past_a_hundred(value, expected):
    assert util.fmt_watts(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "--"),
        (-1, "--"),
        (45, "45s"),
        (90, "1m 30s"),
        (3600 + 22 * 60, "1h 22m"),
        (3 * 86400 + 14 * 3600 + 22 * 60, "3d 14h 22m"),
    ],
)
def test_fmt_duration(value, expected):
    assert util.fmt_duration(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(None, "--"), (999, "999"), (1500, "1.5k"), (2_500_000, "2.5M"), (3_000_000_000, "3.0G")],
)
def test_fmt_count(value, expected):
    assert util.fmt_count(value) == expected


def test_fmt_pct_and_temp():
    assert util.fmt_pct(None) == "--"
    assert util.fmt_pct(49.4) == "49%"
    assert util.fmt_pct(49.44, precision=1) == "49.4%"
    assert util.fmt_temp(None) == "--"
    assert util.fmt_temp(69.6) == "70°C"


def test_clamp():
    assert util.clamp(150.0) == 100.0
    assert util.clamp(-5.0) == 0.0
    assert util.clamp(42.0) == 42.0
    assert util.clamp(5.0, low=10.0, high=20.0) == 10.0
