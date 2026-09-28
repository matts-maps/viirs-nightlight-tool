"""
Offline smoke tests — no Earth Engine account or network access needed.

Run with:
    python -m pytest tests/
or just:
    python tests/test_nightlight_tool.py
"""

import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nightlight_tool import (
    build_breakdown_row,
    build_periods,
    qa_flag,
    summarize_pixels,
    write_csv,
)


def test_build_periods_monthly():
    periods = build_periods("2022-01-01", "2022-04-01", "monthly")
    labels = [p.label for p in periods]
    assert labels == ["2022-01", "2022-02", "2022-03"]
    assert periods[0].start == date(2022, 1, 1)
    assert periods[0].end == date(2022, 2, 1)


def test_build_periods_monthly_partial_start():
    # Starting mid-month should still snap to full calendar months.
    periods = build_periods("2022-01-15", "2022-03-01", "monthly")
    assert [p.label for p in periods] == ["2022-01", "2022-02"]


def test_build_periods_annual():
    periods = build_periods("2020-06-01", "2023-01-01", "annual")
    assert [p.label for p in periods] == ["2020", "2021", "2022"]


def test_build_periods_daily():
    periods = build_periods("2022-01-01", "2022-01-04", "daily")
    assert [p.label for p in periods] == ["2022-01-01", "2022-01-02", "2022-01-03"]
    assert periods[-1].end == date(2022, 1, 4)


def test_build_periods_rejects_bad_range():
    try:
        build_periods("2022-01-01", "2021-01-01", "monthly")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for end <= start")


def test_build_periods_rejects_bad_freq():
    try:
        build_periods("2022-01-01", "2022-02-01", "weekly")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unsupported freq")


def test_qa_flag_no_data():
    assert qa_flag(scene_count=0, valid_pixel_fraction=None) == "no_data"


def test_qa_flag_low_valid_pixels():
    assert qa_flag(scene_count=1, valid_pixel_fraction=0.1) == "low_valid_pixels"


def test_qa_flag_ok():
    assert qa_flag(scene_count=1, valid_pixel_fraction=0.9) == "ok"
    # None fraction means "not tracked for this path" -> not itself a flag.
    assert qa_flag(scene_count=3, valid_pixel_fraction=None) == "ok"


def test_summarize_pixels_all_valid():
    values = np.array([[1.0, 2.0], [3.0, 4.0]])
    mask = np.ones_like(values, dtype=bool)
    stats = summarize_pixels(values, mask)
    assert stats["mean_radiance"] == 2.5
    assert stats["sum_radiance"] == 10.0
    assert stats["median_radiance"] == 2.5
    assert stats["valid_pixel_count"] == 4
    assert stats["total_pixel_count"] == 4
    assert stats["valid_pixel_fraction"] == 1.0


def test_summarize_pixels_partial_mask():
    values = np.array([10.0, 20.0, 30.0, 40.0])
    mask = np.array([True, True, False, False])
    stats = summarize_pixels(values, mask)
    assert stats["mean_radiance"] == 15.0
    assert stats["valid_pixel_count"] == 2
    assert stats["total_pixel_count"] == 4
    assert stats["valid_pixel_fraction"] == 0.5


def test_summarize_pixels_no_valid_pixels():
    values = np.array([1.0, 2.0, 3.0])
    mask = np.array([False, False, False])
    stats = summarize_pixels(values, mask)
    assert stats["mean_radiance"] is None
    assert stats["valid_pixel_count"] == 0
    assert stats["valid_pixel_fraction"] == 0.0


def test_write_csv_roundtrip(tmp_path):
    rows = [
        {"period": "2022-01", "mean_radiance": 1.23, "qa_flag": "ok"},
        {"period": "2022-02", "mean_radiance": 4.56, "qa_flag": "low_valid_pixels"},
    ]
    out = tmp_path / "out.csv"
    write_csv(rows, out)
    text = out.read_text()
    assert "period,mean_radiance,qa_flag" in text
    assert "2022-01,1.23,ok" in text


def test_build_breakdown_row_admin1_ok():
    props = {
        "unit_name": "Sana'a",
        "ADM0_NAME": "Yemen",
        "ADM1_NAME": "Sana'a",
        "avg_rad_mean": 3.2,
        "avg_rad_sum": 1280.0,
        "avg_rad_median": 1.1,
        "avg_rad_count": 400,
    }
    row = build_breakdown_row("2022-01", "avg_rad", props, scene_count=1)
    assert row["unit_name"] == "Sana'a"
    assert row["admin0_name"] == "Yemen"
    assert row["admin1_name"] == "Sana'a"
    assert row["admin2_name"] is None
    assert row["mean_radiance"] == 3.2
    assert row["sum_radiance"] == 1280.0
    assert row["median_radiance"] == 1.1
    assert row["valid_pixel_count"] == 400
    assert row["scene_count"] == 1
    assert row["qa_flag"] == "ok"


def test_build_breakdown_row_admin2_carries_parent_name():
    props = {
        "unit_name": "Some District",
        "ADM0_NAME": "Yemen",
        "ADM1_NAME": "Some Governorate",
        "ADM2_NAME": "Some District",
        "avg_rad_mean": 0.5,
        "avg_rad_sum": 20.0,
        "avg_rad_median": 0.2,
        "avg_rad_count": 40,
    }
    row = build_breakdown_row("2022", "avg_rad", props, scene_count=12)
    assert row["admin1_name"] == "Some Governorate"
    assert row["admin2_name"] == "Some District"


def test_build_breakdown_row_unprefixed_stat_keys():
    # reduceRegions has been observed returning bare reducer output names
    # ("mean") rather than band-prefixed ones ("avg_rad_mean") for a
    # single-band image — this is the exact bug hit against real Yemen data,
    # where every stat column came back blank because only the prefixed key
    # was checked.
    props = {
        "unit_name": "Aden",
        "ADM0_NAME": "Yemen",
        "ADM1_NAME": "Aden",
        "mean": 4.4,
        "sum": 900.0,
        "median": 1.5,
        "count": 200,
    }
    row = build_breakdown_row("2022", "avg_rad", props, scene_count=12)
    assert row["mean_radiance"] == 4.4
    assert row["sum_radiance"] == 900.0
    assert row["median_radiance"] == 1.5
    assert row["valid_pixel_count"] == 200


def test_build_breakdown_row_no_data():
    # Mirrors the no-data path: only unit_name is known, everything else is None.
    row = build_breakdown_row("2022-01", "avg_rad", {"unit_name": "Empty Unit"}, scene_count=0)
    assert row["unit_name"] == "Empty Unit"
    assert row["mean_radiance"] is None
    assert row["scene_count"] == 0
    assert row["qa_flag"] == "no_data"


def test_write_csv_rejects_empty(tmp_path):
    try:
        write_csv([], tmp_path / "out.csv")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty rows")


if __name__ == "__main__":
    # Allow `python tests/test_nightlight_tool.py` without pytest installed.
    import inspect
    import traceback

    module = sys.modules[__name__]
    tests = [
        (name, fn)
        for name, fn in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_")
    ]
    passed, failed = 0, 0
    for name, fn in tests:
        try:
            sig = inspect.signature(fn)
            if "tmp_path" in sig.parameters:
                import tempfile

                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS  {name}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"FAIL  {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
