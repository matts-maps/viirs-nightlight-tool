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
    build_arg_parser,
    build_breakdown_row,
    build_periods,
    list_file_fields,
    qa_flag,
    resolve_breakdown_collection,
    resolve_iso3_candidate_names,
    select_breakdown_chart_units,
    simplify_geometry,
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
        build_periods("2022-01-01", "2022-02-01", "fortnightly")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unsupported freq")


def test_build_periods_weekly():
    # 2022-01-03 is a Monday (ISO week 1). Range covers three full ISO weeks.
    periods = build_periods("2022-01-03", "2022-01-24", "weekly")
    assert [p.label for p in periods] == ["2022-W01", "2022-W02", "2022-W03"]
    assert periods[0].start == date(2022, 1, 3)
    assert periods[0].end == date(2022, 1, 10)


def test_build_periods_weekly_snaps_partial_start_to_monday():
    # Starting mid-week (Thursday) should still snap back to that week's Monday,
    # the same way a mid-month start snaps back to the 1st for "monthly".
    periods = build_periods("2022-01-06", "2022-01-09", "weekly")
    assert [p.label for p in periods] == ["2022-W01"]
    assert periods[0].start == date(2022, 1, 3)


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


def test_build_breakdown_row_attribute_fields_override_defaults():
    # When attribute_fields is given, output uses exactly those columns instead
    # of the hardcoded admin0/1/2 columns -- this is what lets --aoi-file users
    # disambiguate same-named units (e.g. two municipios called the same thing
    # in different states) by picking whichever parent field their data has.
    props = {
        "unit_name": "Independencia",
        "ADM0_NAME": "Venezuela",
        "ADM1_NAME": "Miranda",
        "state_code": "MI",
        "avg_rad_mean": 2.1,
        "avg_rad_sum": 84.0,
        "avg_rad_median": 1.9,
        "avg_rad_count": 40,
    }
    row = build_breakdown_row(
        "2024-01", "avg_rad", props, scene_count=1, attribute_fields=["ADM1_NAME", "state_code"]
    )
    assert row["ADM1_NAME"] == "Miranda"
    assert row["state_code"] == "MI"
    assert "admin0_name" not in row
    assert "admin1_name" not in row
    assert row["mean_radiance"] == 2.1


def test_build_breakdown_row_attribute_fields_missing_property_is_none():
    row = build_breakdown_row(
        "2024-01", "avg_rad", {"unit_name": "X"}, scene_count=1, attribute_fields=["ADM1_NAME"]
    )
    assert row["ADM1_NAME"] is None


def test_build_breakdown_row_no_data():
    # Mirrors the no-data path: only unit_name is known, everything else is None.
    row = build_breakdown_row("2022-01", "avg_rad", {"unit_name": "Empty Unit"}, scene_count=0)
    assert row["unit_name"] == "Empty Unit"
    assert row["mean_radiance"] is None
    assert row["scene_count"] == 0
    assert row["qa_flag"] == "no_data"


def test_build_breakdown_row_carries_unit_id():
    # unit_id is the stable/unique identifier (a GAUL ADM*_CODE, or whichever
    # column the user pointed --unit-id-field at, e.g. a pcode) -- surfaced as
    # its own column separately from attribute_fields, since unlike unit_name
    # it's meant to be unique even when names collide.
    props = {"unit_name": "Independencia", "unit_id": "VE1301", "avg_rad_mean": 2.1}
    row = build_breakdown_row("2024-01", "avg_rad", props, scene_count=1)
    assert row["unit_id"] == "VE1301"


def test_build_breakdown_row_unit_id_defaults_to_none():
    # No unit_id_field was set, so unit_id is just absent from the source
    # properties -- the column should still exist in the row, as None, not
    # be silently dropped.
    row = build_breakdown_row("2024-01", "avg_rad", {"unit_name": "X"}, scene_count=1)
    assert row["unit_id"] is None


def test_simplify_geometry_none_tolerance_is_noop():
    from shapely.geometry import Point

    geom = Point(0, 0).buffer(1.0, quad_segs=64)  # complex circle, many vertices
    result = simplify_geometry(geom, None)
    assert result is geom


def test_simplify_geometry_zero_or_negative_tolerance_is_noop():
    from shapely.geometry import Point

    geom = Point(0, 0).buffer(1.0, quad_segs=64)
    assert simplify_geometry(geom, 0) is geom
    assert simplify_geometry(geom, -0.5) is geom


def test_simplify_geometry_reduces_vertex_count():
    from shapely.geometry import Point

    geom = Point(0, 0).buffer(1.0, quad_segs=64)  # ~256 vertices
    original_vertex_count = len(geom.exterior.coords)

    simplified = simplify_geometry(geom, 0.05)
    simplified_vertex_count = len(simplified.exterior.coords)

    assert simplified_vertex_count < original_vertex_count
    # Simplification shouldn't distort a smooth circle beyond recognition —
    # area should stay close to the original.
    assert simplified.area == pytest_approx(geom.area, rel=0.05)


def pytest_approx(value, rel):
    # Tiny local stand-in so this test file keeps working without pytest
    # installed (see the __main__ runner below).
    class _Approx:
        def __eq__(self, other):
            if value == 0:
                return abs(other) <= rel
            return abs(other - value) <= abs(value) * rel

    return _Approx()


def test_resolve_iso3_candidate_names_simple_case():
    # Ukraine is the easy case -- pycountry's plain name matches GAUL's
    # ADM0_NAME exactly, no fallback needed.
    assert resolve_iso3_candidate_names("UKR") == ["Ukraine"]


def test_resolve_iso3_candidate_names_is_case_insensitive_input():
    assert resolve_iso3_candidate_names("ukr") == ["Ukraine"]


def test_resolve_iso3_candidate_names_multiple_candidates_for_tricky_country():
    # South Korea is the case that motivates trying several candidates: none
    # of pycountry's fields alone reliably matches whatever GAUL happens to
    # use, so all non-empty name fields should come back for the caller to
    # try in turn.
    candidates = resolve_iso3_candidate_names("KOR")
    assert "South Korea" in candidates  # common_name
    assert any("Korea" in c for c in candidates)


def test_resolve_iso3_candidate_names_rejects_unknown_code():
    try:
        resolve_iso3_candidate_names("XXX")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unrecognised ISO3 code")


def _breakdown_rows_for(unit_names):
    return [{"unit_name": name, "period": "2022-01"} for name in unit_names]


def test_select_breakdown_chart_units_no_filter_first_seen_order():
    rows = _breakdown_rows_for(["Kyiv", "Odesa", "Kyiv", "Lviv"])
    assert select_breakdown_chart_units(rows) == ["Kyiv", "Odesa", "Lviv"]


def test_select_breakdown_chart_units_respects_max_panels_cap():
    rows = _breakdown_rows_for([f"unit{i}" for i in range(50)])
    units = select_breakdown_chart_units(rows, max_panels=5)
    assert units == [f"unit{i}" for i in range(5)]


def test_select_breakdown_chart_units_explicit_filter_ignores_cap():
    # A deliberate --chart-units selection is never truncated, even if it's
    # longer than max_panels -- the cap only protects the "chart everything"
    # default from producing hundreds of unreadable panels.
    rows = _breakdown_rows_for([f"unit{i}" for i in range(10)])
    requested = [f"unit{i}" for i in range(8)]
    units = select_breakdown_chart_units(rows, unit_filter=requested, max_panels=3)
    assert units == requested


def test_select_breakdown_chart_units_filter_drops_unknown_names():
    rows = _breakdown_rows_for(["Kyiv", "Odesa"])
    units = select_breakdown_chart_units(rows, unit_filter=["Kyiv", "Nonexistent"])
    assert units == ["Kyiv"]


def test_list_file_fields_excludes_geometry():
    # sample_aoi/toy_bbox.geojson has properties "name" and "note" -- this is
    # what the wizard shows the user before asking which column names each
    # unit or which fields to include as --attributes.
    fields = list_file_fields(str(Path(__file__).resolve().parent.parent / "sample_aoi" / "toy_bbox.geojson"))
    assert "geometry" not in fields
    assert "name" in fields
    assert "note" in fields


def test_build_arg_parser_accepts_admin3_through_5_for_aoi_file():
    # --aoi-file breakdown just uses the admin level as an output label, so
    # nothing stops finer levels than GAUL (which tops out at admin2).
    p = build_arg_parser()
    for level in ("admin3", "admin4", "admin5"):
        args = p.parse_args(
            [
                "--aoi-file", "x.geojson",
                "--unit-name-field", "name",
                "--breakdown", level,
                "--start", "2024-01-01",
                "--end", "2024-02-01",
                "--freq", "monthly",
                "--out", "o.csv",
            ]
        )
        assert args.breakdown == level


def test_resolve_breakdown_collection_rejects_unsupported_gaul_level():
    # FAO GAUL 2015 only carries admin1/admin2 below country level -- asking
    # for admin3+ with --aoi-name should fail fast with a clear message
    # pointing at --aoi-file, not a raw KeyError from the level lookup table.
    import sys
    import types

    sys.modules.setdefault("ee", types.ModuleType("ee"))
    try:
        resolve_breakdown_collection(None, "Ukraine", "admin3", None)
    except ValueError as e:
        assert "admin3" in str(e)
        assert "aoi-file" in str(e)
    else:
        raise AssertionError("expected ValueError for unsupported GAUL admin level")


def test_write_csv_rejects_empty(tmp_path):
    try:
        write_csv([], tmp_path / "out.csv")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty rows")


class _FakeAggregation:
    def __init__(self, values):
        self._values = values

    def getInfo(self):
        return self._values


class _FakeFeatureCollection:
    """Stand-in for an ee.FeatureCollection: just enough for
    fetch_period_breakdown_stats' no-image branch to aggregate columns off
    of, without needing a real Earth Engine session."""

    def __init__(self, columns: dict):
        self._columns = columns

    def aggregate_array(self, field):
        if field not in self._columns:
            raise KeyError(field)
        return _FakeAggregation(self._columns[field])


def test_fetch_period_breakdown_stats_no_image_still_carries_attribute_fields():
    # Regression test: when Earth Engine has no VIIRS composite yet for a
    # period (e.g. the most recent month, before it's been published), the
    # no-image branch used to only pull unit_name/unit_id off the feature
    # collection -- every --attributes column (admin0_name, admin0_pcode,
    # etc.) silently came back None for that period's rows instead of being
    # carried through like every other period.
    import types

    import nightlight_tool as nt

    fc = _FakeFeatureCollection(
        {
            "unit_name": ["Independencia", "Sucre"],
            "unit_id": ["VE1301", "VE1302"],
            "adm0_name": ["Venezuela", "Venezuela"],
            "adm0_pcode": ["VE", "VE"],
        }
    )
    period = nt.build_periods("2026-08-01", "2026-09-01", "monthly")[0]

    original = nt._get_period_image_and_scene_count
    nt._get_period_image_and_scene_count = lambda freq, period: (None, "avg_rad", 0)
    try:
        rows = nt.fetch_period_breakdown_stats(
            "monthly", fc, period, attribute_fields=["adm0_name", "adm0_pcode"]
        )
    finally:
        nt._get_period_image_and_scene_count = original

    assert len(rows) == 2
    assert [r["unit_name"] for r in rows] == ["Independencia", "Sucre"]
    assert [r["unit_id"] for r in rows] == ["VE1301", "VE1302"]
    assert [r["adm0_name"] for r in rows] == ["Venezuela", "Venezuela"]
    assert [r["adm0_pcode"] for r in rows] == ["VE", "VE"]
    assert all(r["qa_flag"] == "no_data" for r in rows)


def test_select_breakdown_chart_units_dedupes_by_unit_id_not_name():
    # Two different units sharing a unit_name (common at admin3 -- village/
    # ward names repeat across different districts) must stay two distinct
    # chart panels, not collapse into one.
    rows = [
        {"unit_name": "San Jose", "unit_id": "A1", "period": "2026-01"},
        {"unit_name": "San Jose", "unit_id": "A2", "period": "2026-01"},
        {"unit_name": "Miraflores", "unit_id": "A3", "period": "2026-01"},
    ]
    units = select_breakdown_chart_units(rows)
    assert units == ["A1", "A2", "A3"]


def test_select_breakdown_chart_units_filter_by_name_returns_all_matching_ids():
    # --chart-units still takes unit_name values, but when a name is shared
    # by several distinct units, all of them should be selected rather than
    # arbitrarily picking (or merging) just one.
    rows = [
        {"unit_name": "San Jose", "unit_id": "A1", "period": "2026-01"},
        {"unit_name": "San Jose", "unit_id": "A2", "period": "2026-01"},
        {"unit_name": "Miraflores", "unit_id": "A3", "period": "2026-01"},
    ]
    units = select_breakdown_chart_units(rows, unit_filter=["San Jose"])
    assert units == ["A1", "A2"]


def test_prompt_field_list_retry_prefills_valid_names_not_full_retype():
    # Regression test: a real user typed a 6-field list with one typo
    # ("admn0_pcode"), got told to try again, and then -- since the retry
    # prompt gave no hint of what to keep -- retyped only 2 fields, silently
    # dropping the 4 he actually wanted. The retry should instead default to
    # the fields that *did* validate, so pressing Enter keeps them.
    import builtins

    import nightlight_tool as nt

    responses = iter(
        [
            "adm2_name,adm2_pcode, adm1_name, adm1_pcode, adm0_name, admn0_pcode",
            "",  # accept the pre-filled default on retry
        ]
    )
    prompts_shown = []

    def fake_input(prompt=""):
        prompts_shown.append(prompt)
        return next(responses)

    original_input = builtins.input
    builtins.input = fake_input
    try:
        result = nt._prompt_field_list(
            "Extra attribute columns",
            ["adm2_name", "adm2_pcode", "adm1_name", "adm1_pcode", "adm0_name", "adm0_pcode"],
        )
    finally:
        builtins.input = original_input

    assert result == "adm2_name, adm2_pcode, adm1_name, adm1_pcode, adm0_name"
    # the retry prompt should show the valid fields as its default, not be blank
    assert "adm2_name, adm2_pcode, adm1_name, adm1_pcode, adm0_name" in prompts_shown[1]


def test_select_breakdown_chart_units_falls_back_to_name_without_unit_id():
    # No unit_id at all (older runs, or --aoi-file without --unit-id-field)
    # -- behaviour is unchanged from before: group/dedupe by name.
    rows = [{"unit_name": name, "period": "2022-01"} for name in ["Kyiv", "Odesa", "Kyiv"]]
    units = select_breakdown_chart_units(rows)
    assert units == ["Kyiv", "Odesa"]


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
