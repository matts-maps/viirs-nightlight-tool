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
    DEFAULT_DARK_THRESHOLD_NW,
    Period,
    attach_geometry,
    build_arg_parser,
    build_argv_from_form,
    build_breakdown_row,
    build_periods,
    build_output_filename,
    build_raster_filename,
    compute_year_over_year_change,
    list_file_fields,
    parse_period_label,
    qa_flag,
    rename_unit_columns,
    resolve_breakdown_collection,
    resolve_geoextent,
    select_breakdown_chart_units,
    shapefile_safe_field_names,
    simplify_geometry,
    split_rows_by_period,
    summarize_pixels,
    write_csv,
    write_geo_outputs_combined,
    write_geo_outputs_per_period,
    year_ago_period,
    year_ago_period_label,
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


def test_build_breakdown_row_ok():
    props = {
        "unit_name": "Sana'a",
        "unit_id": "YE-SAN",
        "avg_rad_mean": 3.2,
        "avg_rad_sum": 1280.0,
        "avg_rad_median": 1.1,
        "avg_rad_count": 400,
        "dark_mean": 0.25,
    }
    row = build_breakdown_row("2022-01", "avg_rad", props, scene_count=1)
    assert row["unit_name"] == "Sana'a"
    assert row["unit_id"] == "YE-SAN"
    assert row["mean_radiance"] == 3.2
    assert row["sum_radiance"] == 1280.0
    assert row["median_radiance"] == 1.1
    assert row["pct_dark"] == 25.0
    assert row["valid_pixel_count"] == 400
    assert row["scene_count"] == 1
    assert row["qa_flag"] == "ok"


def test_build_breakdown_row_pct_dark_none_when_not_reduced():
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
    assert row["pct_dark"] is None


def test_build_breakdown_row_carries_requested_attribute_fields():
    props = {
        "unit_name": "Some District",
        "ADM0_NAME": "Yemen",
        "ADM1_NAME": "Some Governorate",
        "avg_rad_mean": 0.5,
        "avg_rad_sum": 20.0,
        "avg_rad_median": 0.2,
        "avg_rad_count": 40,
    }
    row = build_breakdown_row(
        "2022",
        "avg_rad",
        props,
        scene_count=12,
        attribute_fields=["ADM0_NAME", "ADM1_NAME"],
    )
    assert row["ADM0_NAME"] == "Yemen"
    assert row["ADM1_NAME"] == "Some Governorate"


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


def test_build_arg_parser_accepts_admin3_through_5():
    # Breakdown just uses the admin level as an output label, so nothing
    # stops finer levels -- any of admin1-5 works, however the boundary
    # file is actually organized.
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
                "--out-dir", "out",
                "--geoextent", "x",
            ]
        )
        assert args.breakdown == level


def test_resolve_breakdown_collection_requires_unit_name_field():
    import sys
    import types

    sys.modules.setdefault("ee", types.ModuleType("ee"))
    try:
        resolve_breakdown_collection("x.geojson", "admin3", None)
    except ValueError as e:
        assert "unit-name-field" in str(e)
    else:
        raise AssertionError("expected ValueError when --unit-name-field is missing")


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


# ---------------------------------------------------------------------------
# build_argv_from_form -- the GUI's argv-building core (no Tkinter involved)
# ---------------------------------------------------------------------------

def test_build_argv_from_form_minimal_no_breakdown():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-04-01",
            "freq": "monthly",
            "out_dir": "out",
        }
    )
    assert argv == [
        "--aoi-file", "aoi.geojson",
        "--start", "2026-01-01",
        "--end", "2026-04-01",
        "--freq", "monthly",
        "--out-dir", "out",
        "--geoextent", "crm",
    ]
    # and it should be accepted by the real parser, not just look plausible
    args = build_arg_parser().parse_args(argv)
    assert args.aoi_file == "aoi.geojson"
    assert args.breakdown is None


def test_build_argv_from_form_requires_geoextent():
    # --aoi-file has no code of its own to default to -- --geoextent is
    # always required.
    try:
        build_argv_from_form(
            {
                "aoi_file": "aoi.geojson",
                "start": "2026-01-01",
                "end": "2026-02-01",
                "freq": "monthly",
                "out_dir": "out",
            }
        )
    except ValueError as e:
        assert "geoextent" in str(e).lower()
    else:
        raise AssertionError("expected ValueError with no geoextent code")


def test_build_argv_from_form_requires_aoi_file():
    try:
        build_argv_from_form(
            {
                "geoextent": "crm",
                "start": "2026-01-01",
                "end": "2026-02-01",
                "freq": "monthly",
                "out_dir": "out",
            }
        )
    except ValueError as e:
        assert "boundary file" in str(e).lower()
    else:
        raise AssertionError("expected ValueError with no boundary file")


def test_build_argv_from_form_file_breakdown_requires_unit_name_field():
    try:
        build_argv_from_form(
            {
                "aoi_file": "aoi.geojson",
                "geoextent": "crm",
                "breakdown_level": 3,
                "start": "2026-01-01",
                "end": "2026-02-01",
                "freq": "monthly",
                "out_dir": "out",
            }
        )
    except ValueError as e:
        assert "unit" in str(e).lower()
    else:
        raise AssertionError("expected ValueError when unit_name_field is missing")


def test_build_argv_from_form_file_breakdown_admin3_with_all_fields():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "VEN",
            "breakdown_level": 3,
            "unit_name_field": "adm3_name",
            "unit_id_field": "adm3_pcode",
            "simplify_tolerance": "0.001",
            "attributes": "adm0_name, adm0_pcode",
            "start": "2026-05-01",
            "end": "2026-09-01",
            "freq": "monthly",
            "out_dir": "out",
            "chart": True,
            "chart_units": ["Independencia", "Sucre"],
        }
    )
    args = build_arg_parser().parse_args(argv)
    assert args.aoi_file == "aoi.geojson"
    assert args.breakdown == "admin3"
    assert args.unit_name_field == "adm3_name"
    assert args.unit_id_field == "adm3_pcode"
    assert args.simplify_tolerance == 0.001
    assert args.attributes == "adm0_name, adm0_pcode"
    assert args.chart is True
    assert args.chart_units == "Independencia,Sucre"


def test_build_argv_from_form_chart_units_ignored_without_breakdown():
    # --chart-units only makes sense alongside --breakdown -- a whole-AOI
    # chart has exactly one line, there's nothing to filter by unit.
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "chart": True,
            "chart_units": ["should", "be", "ignored"],
        }
    )
    assert "--chart-units" not in argv
    assert "--chart" in argv


def test_build_argv_from_form_requires_dates_and_out_dir():
    for missing in ("start", "end", "out_dir"):
        fields = {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
        }
        fields[missing] = ""
        try:
            build_argv_from_form(fields)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError with {missing!r} missing")


def test_build_argv_from_form_vector_out_included_when_given():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "vector_out": "geojson",
        }
    )
    args = build_arg_parser().parse_args(argv)
    assert args.vector_out == "geojson"


def test_build_argv_from_form_vector_out_shapefile():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "vector_out": "shapefile",
        }
    )
    args = build_arg_parser().parse_args(argv)
    assert args.vector_out == "shapefile"


def test_build_argv_from_form_vector_out_omitted_when_none():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
        }
    )
    assert "--vector-out" not in argv
    args = build_arg_parser().parse_args(argv)
    assert args.vector_out is None


def test_build_argv_from_form_include_yoy_included_when_checked():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "include_yoy": True,
        }
    )
    args = build_arg_parser().parse_args(argv)
    assert args.include_yoy is True


def test_build_argv_from_form_include_yoy_omitted_when_unchecked():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
        }
    )
    assert "--include-yoy" not in argv
    args = build_arg_parser().parse_args(argv)
    assert args.include_yoy is False


def test_build_argv_from_form_dark_threshold_included_when_given():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "dark_threshold": "0.75",
        }
    )
    args = build_arg_parser().parse_args(argv)
    assert args.dark_threshold == 0.75


def test_build_argv_from_form_dark_threshold_omitted_uses_default():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
        }
    )
    assert "--dark-threshold" not in argv
    args = build_arg_parser().parse_args(argv)
    assert args.dark_threshold == DEFAULT_DARK_THRESHOLD_NW


def test_build_argv_from_form_dark_threshold_rejects_non_number():
    try:
        build_argv_from_form(
            {
                "aoi_file": "aoi.geojson",
                "geoextent": "crm",
                "start": "2026-01-01",
                "end": "2026-02-01",
                "freq": "monthly",
                "out_dir": "out",
                "dark_threshold": "not-a-number",
            }
        )
    except ValueError as e:
        assert "number" in str(e)
    else:
        raise AssertionError("expected ValueError for a non-numeric dark_threshold")


def test_extract_pct_dark_reads_dark_mean():
    from nightlight_tool import _extract_pct_dark

    assert _extract_pct_dark({"dark_mean": 0.4}) == 40.0
    assert _extract_pct_dark({}) is None


# ---------------------------------------------------------------------------
# --raster-whole-aoi/--raster-yoy: filename building and geoextent resolution
# ---------------------------------------------------------------------------


def test_sanitize_filename_token_replaces_non_alnum_with_underscores():
    from nightlight_tool import _sanitize_filename_token

    assert _sanitize_filename_token("2026-01") == "2026_01"
    assert _sanitize_filename_token("2026-W05") == "2026_w05"
    assert _sanitize_filename_token("UKR") == "ukr"
    assert _sanitize_filename_token("a -- b  c") == "a_b_c"
    assert _sanitize_filename_token("__leading and trailing__") == "leading_and_trailing"


def test_sanitize_filename_token_empty_input_gives_na():
    from nightlight_tool import _sanitize_filename_token

    assert _sanitize_filename_token("") == "na"
    assert _sanitize_filename_token("---") == "na"


def test_build_raster_filename_uses_dnc_style_template():
    assert (
        build_raster_filename("UKR", "2026-01")
        == "ukr_evnt_lit_ras_s0_viirs_pp_2026_01.tif"
    )


def test_build_raster_filename_all_underscores_no_hyphens():
    name = build_raster_filename("crm", "diff_yoy_2026-01_minus_2025-01")
    assert "-" not in name
    assert name == "crm_evnt_lit_ras_s0_viirs_pp_diff_yoy_2026_01_minus_2025_01.tif"


def test_resolve_geoextent_returns_explicit_code():
    assert resolve_geoextent("crm") == "crm"


def test_resolve_geoextent_raises_when_not_given():
    try:
        resolve_geoextent(None)
    except ValueError as e:
        assert "--geoextent" in str(e)
    else:
        raise AssertionError("expected ValueError when no geoextent is given")


def test_build_output_filename_underscore_style():
    assert build_output_filename("NPL", "csv") == "npl_nightlights.csv"
    assert build_output_filename("npl", "geojson") == "npl_nightlights.geojson"


def test_build_argv_from_form_raster_whole_aoi_and_yoy_are_independent():
    # Checking only one of the two raster checkboxes shouldn't turn on the
    # other -- they're independent, per the GUI design.
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "raster_whole_aoi": True,
        }
    )
    args = build_arg_parser().parse_args(argv)
    assert args.raster_whole_aoi is True
    assert args.raster_yoy is False

    argv2 = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "raster_yoy": True,
        }
    )
    args2 = build_arg_parser().parse_args(argv2)
    assert args2.raster_whole_aoi is False
    assert args2.raster_yoy is True


def test_build_argv_from_form_raster_requires_geoextent():
    # The geoextent check happens regardless of raster flags (CSV needs it
    # too) -- this just confirms the raster flags still work once a
    # geoextent code is supplied.
    try:
        build_argv_from_form(
            {
                "aoi_file": "aoi.geojson",
                "start": "2026-01-01",
                "end": "2026-02-01",
                "freq": "monthly",
                "out_dir": "out",
                "raster_whole_aoi": True,
            }
        )
    except ValueError as e:
        assert "geoextent" in str(e).lower()
    else:
        raise AssertionError("expected ValueError without a geoextent code")


def test_build_argv_from_form_raster_out_with_geoextent_ok():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "raster_whole_aoi": True,
        }
    )
    args = build_arg_parser().parse_args(argv)
    assert args.raster_whole_aoi is True
    assert args.geoextent == "crm"


def test_build_argv_from_form_raster_flags_omitted_by_default():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
        }
    )
    assert "--raster-whole-aoi" not in argv
    assert "--raster-yoy" not in argv
    args = build_arg_parser().parse_args(argv)
    assert args.raster_whole_aoi is False
    assert args.raster_yoy is False


def test_build_argv_from_form_raster_scale_included_when_given():
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "raster_whole_aoi": True,
            "raster_scale": "1000",
        }
    )
    args = build_arg_parser().parse_args(argv)
    assert args.raster_scale == 1000


def test_build_argv_from_form_raster_scale_omitted_when_no_raster_flag():
    # raster_scale is only meaningful with raster_whole_aoi/raster_yoy set --
    # confirm it's not passed through on its own (would otherwise be a
    # silently-ignored argparse default with no effect).
    argv = build_argv_from_form(
        {
            "aoi_file": "aoi.geojson",
            "geoextent": "crm",
            "start": "2026-01-01",
            "end": "2026-02-01",
            "freq": "monthly",
            "out_dir": "out",
            "raster_scale": "1000",
        }
    )
    assert "--raster-scale" not in argv


# ---------------------------------------------------------------------------
# rename_unit_columns -- output columns named after the field that actually
# identifies each unit, not a generic label.
# ---------------------------------------------------------------------------

def test_rename_unit_columns_renames_both():
    rows = [
        {"period": "2026-01", "unit_name": "Independencia", "unit_id": "VE1301", "mean_radiance": 1.2},
        {"period": "2026-01", "unit_name": "Sucre", "unit_id": "VE1302", "mean_radiance": 3.4},
    ]
    renamed = rename_unit_columns(rows, "adm2_name", "adm2_pcode")
    assert renamed == [
        {"period": "2026-01", "adm2_name": "Independencia", "adm2_pcode": "VE1301", "mean_radiance": 1.2},
        {"period": "2026-01", "adm2_name": "Sucre", "adm2_pcode": "VE1302", "mean_radiance": 3.4},
    ]
    # key order preserved -- csv.DictWriter takes its header from this
    assert list(renamed[0].keys()) == ["period", "adm2_name", "adm2_pcode", "mean_radiance"]


def test_rename_unit_columns_leaves_unit_id_when_no_id_field_given():
    # No --unit-id-field was set, so unit_id has no more specific name to
    # take -- it should stay 'unit_id', not become e.g. 'None'.
    rows = [{"period": "2026-01", "unit_name": "Independencia", "unit_id": None, "mean_radiance": 1.2}]
    renamed = rename_unit_columns(rows, "adm2_name", None)
    assert renamed == [{"period": "2026-01", "adm2_name": "Independencia", "unit_id": None, "mean_radiance": 1.2}]


def test_rename_unit_columns_noop_when_names_are_already_generic():
    rows = [{"period": "2026-01", "unit_name": "X", "unit_id": "1"}]
    renamed = rename_unit_columns(rows, "unit_name", "unit_id")
    assert renamed == rows


def test_rename_unit_columns_handles_empty_rows():
    assert rename_unit_columns([], "adm2_name", "adm2_pcode") == []


def test_rename_unit_columns_does_not_mutate_original_rows():
    # Charting happens on the original rows (with the generic unit_name/
    # unit_id keys) after the CSV is written -- the rename must not corrupt
    # them for that later use.
    rows = [{"period": "2026-01", "unit_name": "Independencia", "unit_id": "VE1301"}]
    rename_unit_columns(rows, "adm2_name", "adm2_pcode")
    assert rows == [{"period": "2026-01", "unit_name": "Independencia", "unit_id": "VE1301"}]




# --- attach_geometry / shapefile_safe_field_names / split_rows_by_period --
# --- write_geo_outputs_per_period (--geo-out) ------------------------------

def test_attach_geometry_adds_geometry_key_without_mutating_input():
    rows = [{"unit_id": "P1"}, {"unit_id": "P2"}]
    out = attach_geometry(rows, lambda r: f"geom-{r['unit_id']}")
    assert [r["geometry"] for r in out] == ["geom-P1", "geom-P2"]
    assert "geometry" not in rows[0]  # original rows untouched


def test_attach_geometry_missing_lookup_yields_none():
    out = attach_geometry([{"unit_id": "X"}], lambda r: None)
    assert out[0]["geometry"] is None


def test_shapefile_safe_field_names_short_names_pass_through():
    mapping = shapefile_safe_field_names(["period", "qa_flag"])
    assert mapping == {"period": "period", "qa_flag": "qa_flag"}


def test_shapefile_safe_field_names_truncates_long_names():
    mapping = shapefile_safe_field_names(["mean_radiance_2026-01"])
    assert len(mapping["mean_radiance_2026-01"]) == 10
    assert mapping["mean_radiance_2026-01"] == "mean_radia"


def test_shapefile_safe_field_names_resolves_collisions_uniquely():
    # Both truncate to the same 10 characters on their own -- collision
    # resolution must keep every result both <=10 chars and unique.
    names = ["mean_radiance_2026-01", "mean_radiance_2026-02"]
    mapping = shapefile_safe_field_names(names)
    values = list(mapping.values())
    assert len(set(values)) == len(values)
    assert all(len(v) <= 10 for v in values)


def test_shapefile_safe_field_names_many_collisions_stay_unique():
    names = [f"very_long_name_{i}" for i in range(12)]
    mapping = shapefile_safe_field_names(names)
    values = list(mapping.values())
    assert len(set(values)) == len(names)
    assert all(len(v) <= 10 for v in values)


def test_split_rows_by_period_groups_and_preserves_order():
    rows = [
        {"unit": "A", "period": "2026-02", "v": 2},
        {"unit": "A", "period": "2026-01", "v": 1},
        {"unit": "B", "period": "2026-02", "v": 20},
        {"unit": "B", "period": "2026-01", "v": 10},
    ]
    grouped = split_rows_by_period(rows)
    assert list(grouped.keys()) == ["2026-02", "2026-01"]  # first-seen order
    assert len(grouped["2026-01"]) == 2
    assert len(grouped["2026-02"]) == 2
    assert {r["unit"] for r in grouped["2026-01"]} == {"A", "B"}


def test_split_rows_by_period_empty_input():
    assert split_rows_by_period([]) == {}


def test_write_geo_outputs_per_period_writes_one_file_per_period(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    rows_by_period = {
        "2026-01": [
            {"adm2_pcode": "P1", "period": "2026-01", "mean_radiance": 1.0, "geometry": Point(0, 0)},
            {"adm2_pcode": "P2", "period": "2026-01", "mean_radiance": 2.0, "geometry": Point(1, 0)},
        ],
        "2026-02": [
            {"adm2_pcode": "P1", "period": "2026-02", "mean_radiance": 1.5, "geometry": Point(0, 0)},
            {"adm2_pcode": "P2", "period": "2026-02", "mean_radiance": 2.5, "geometry": Point(1, 0)},
        ],
    }
    written = write_geo_outputs_per_period(rows_by_period, tmp_path / "out.geojson")
    assert set(written.keys()) == {"2026-01", "2026-02"}
    assert written["2026-01"] == tmp_path / "out_2026-01.geojson"
    assert written["2026-02"] == tmp_path / "out_2026-02.geojson"
    for path in written.values():
        assert path.exists()

    gdf = gpd.read_file(written["2026-01"])
    assert len(gdf) == 2
    # plain column name, not period-suffixed, since each file is already one period
    assert "mean_radiance" in gdf.columns
    assert "mean_radiance_2026-01" not in gdf.columns
    # "period" itself is dropped by default -- redundant with the filename
    assert "period" not in gdf.columns


def test_write_geo_outputs_per_period_keeps_period_column_when_drop_cols_overridden(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    rows_by_period = {
        "2026-01": [{"adm2_pcode": "P1", "period": "2026-01", "geometry": Point(0, 0)}],
    }
    written = write_geo_outputs_per_period(rows_by_period, tmp_path / "out.geojson", drop_cols=())
    gdf = gpd.read_file(written["2026-01"])
    assert "period" in gdf.columns


def test_write_geo_outputs_per_period_shapefile_applies_field_truncation(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    rows_by_period = {
        "2026-01": [
            {
                "some_very_long_attribute_name": "x",
                "period": "2026-01",
                "geometry": Point(0, 0),
            }
        ],
    }
    written = write_geo_outputs_per_period(rows_by_period, tmp_path / "out.shp")
    gdf = gpd.read_file(written["2026-01"])
    assert all(len(c) <= 10 for c in gdf.columns if c != "geometry")


def test_write_geo_outputs_per_period_rejects_unsupported_extension(tmp_path):
    from shapely.geometry import Point

    rows_by_period = {"2026-01": [{"geometry": Point(0, 0)}]}
    try:
        write_geo_outputs_per_period(rows_by_period, tmp_path / "out.gpkg")
    except ValueError as e:
        assert ".geojson" in str(e) and ".shp" in str(e)
    else:
        raise AssertionError("expected ValueError for an unsupported --geo-out extension")


# --- write_geo_outputs_combined (--geo-out, every period in one file) ------


def test_write_geo_outputs_combined_writes_one_file_with_every_period(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    rows = [
        {"adm2_pcode": "P1", "period": "2026-01", "mean_radiance": 1.0, "geometry": Point(0, 0)},
        {"adm2_pcode": "P2", "period": "2026-01", "mean_radiance": 2.0, "geometry": Point(1, 0)},
        {"adm2_pcode": "P1", "period": "2026-02", "mean_radiance": 1.5, "geometry": Point(0, 0)},
        {"adm2_pcode": "P2", "period": "2026-02", "mean_radiance": 2.5, "geometry": Point(1, 0)},
    ]
    written = write_geo_outputs_combined(rows, tmp_path / "out.geojson")
    assert written == tmp_path / "out_all_periods.geojson"
    assert written.exists()

    gdf = gpd.read_file(written)
    # one feature per (unit, period) pair -- the one-to-many join, same row
    # shape as the CSV -- geometry repeated for each period
    assert len(gdf) == 4
    assert "period" in gdf.columns
    assert sorted(gdf["period"]) == ["2026-01", "2026-01", "2026-02", "2026-02"]
    assert "mean_radiance" in gdf.columns


def test_write_geo_outputs_combined_shapefile_applies_field_truncation(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    rows = [
        {"some_very_long_attribute_name": "x", "period": "2026-01", "geometry": Point(0, 0)},
    ]
    written = write_geo_outputs_combined(rows, tmp_path / "out.shp")
    assert written == tmp_path / "out_all_periods.shp"
    gdf = gpd.read_file(written)
    assert all(len(c) <= 10 for c in gdf.columns if c != "geometry")


def test_write_geo_outputs_combined_rejects_unsupported_extension(tmp_path):
    from shapely.geometry import Point

    rows = [{"geometry": Point(0, 0)}]
    try:
        write_geo_outputs_combined(rows, tmp_path / "out.gpkg")
    except ValueError as e:
        assert ".geojson" in str(e) and ".shp" in str(e)
    else:
        raise AssertionError("expected ValueError for an unsupported --geo-out extension")


def test_parse_period_label_monthly_roundtrip():
    for period in build_periods("2021-11-01", "2022-03-01", "monthly"):
        assert parse_period_label(period.label, "monthly") == period


def test_parse_period_label_annual_roundtrip():
    for period in build_periods("2019-01-01", "2023-01-01", "annual"):
        assert parse_period_label(period.label, "annual") == period


def test_parse_period_label_weekly_roundtrip():
    for period in build_periods("2022-01-01", "2022-04-01", "weekly"):
        assert parse_period_label(period.label, "weekly") == period


def test_parse_period_label_daily_roundtrip():
    for period in build_periods("2022-02-25", "2022-03-05", "daily"):
        assert parse_period_label(period.label, "daily") == period


def test_parse_period_label_rejects_wrong_format():
    try:
        parse_period_label("not-a-period", "monthly")
    except ValueError as e:
        assert "monthly" in str(e)
    else:
        raise AssertionError("expected ValueError for an unparseable period label")


def test_year_ago_period_monthly():
    period = parse_period_label("2022-03", "monthly")
    assert year_ago_period(period, "monthly") == parse_period_label("2021-03", "monthly")


def test_year_ago_period_annual():
    period = parse_period_label("2022", "annual")
    assert year_ago_period(period, "annual") == parse_period_label("2021", "annual")


def test_year_ago_period_daily():
    period = parse_period_label("2022-03-14", "daily")
    assert year_ago_period(period, "daily") == parse_period_label("2021-03-14", "daily")


def test_year_ago_period_daily_feb29_has_no_match():
    # 2024 is a leap year (Feb 29 exists); 2023 is not, so there's no
    # year-ago Feb 29 to compare against.
    period = parse_period_label("2024-02-29", "daily")
    assert year_ago_period(period, "daily") is None


def test_year_ago_period_weekly():
    period = parse_period_label("2022-W10", "weekly")
    assert year_ago_period(period, "weekly") == parse_period_label("2021-W10", "weekly")


def test_year_ago_period_weekly_week53_has_no_match():
    # 2026 has an ISO week 53; 2025 does not, so week 53 has no year-ago match.
    period = parse_period_label("2026-W53", "weekly")
    assert year_ago_period(period, "weekly") is None


def test_year_ago_period_label_wraps_year_ago_period():
    assert year_ago_period_label("2022-03", "monthly") == "2021-03"
    assert year_ago_period_label("2026-W53", "weekly") is None
    assert year_ago_period_label("garbage", "monthly") is None


def test_compute_year_over_year_change_whole_aoi():
    rows = [
        {"period": "2021-03", "mean_radiance": 10.0, "sum_radiance": 100.0},
        {"period": "2022-03", "mean_radiance": 12.0, "sum_radiance": 90.0},
    ]
    lookup = {(None, r["period"]): r for r in rows}
    out = compute_year_over_year_change(rows, "monthly", lookup)

    assert out[0]["mean_radiance_yoy_abs"] is None  # nothing a year before 2021-03 was fetched
    assert out[0]["mean_radiance_yoy_pct"] is None

    assert out[1]["mean_radiance_yoy_abs"] == 2.0
    assert round(out[1]["mean_radiance_yoy_pct"], 4) == 20.0
    assert out[1]["sum_radiance_yoy_abs"] == -10.0


def test_compute_year_over_year_change_zero_previous_gives_abs_but_not_pct():
    rows = [
        {"period": "2021-03", "mean_radiance": 0.0, "sum_radiance": 0.0},
        {"period": "2022-03", "mean_radiance": 0.1, "sum_radiance": 5.0},
    ]
    lookup = {(None, r["period"]): r for r in rows}
    out = compute_year_over_year_change(rows, "monthly", lookup)
    assert out[1]["mean_radiance_yoy_abs"] == 0.1
    assert out[1]["mean_radiance_yoy_pct"] is None


def test_compute_year_over_year_change_respects_group_key():
    rows = [
        {"period": "2021-03", "unit_name": "A", "mean_radiance": 5.0},
        {"period": "2021-03", "unit_name": "B", "mean_radiance": 50.0},
        {"period": "2022-03", "unit_name": "A", "mean_radiance": 8.0},
        {"period": "2022-03", "unit_name": "B", "mean_radiance": 40.0},
    ]

    def group_key(row):
        return row["unit_name"]

    lookup = {(group_key(r), r["period"]): r for r in rows}
    out = compute_year_over_year_change(rows, "monthly", lookup, group_key=group_key)

    a_2022 = next(r for r in out if r["unit_name"] == "A" and r["period"] == "2022-03")
    b_2022 = next(r for r in out if r["unit_name"] == "B" and r["period"] == "2022-03")
    assert a_2022["mean_radiance_yoy_abs"] == 3.0
    assert b_2022["mean_radiance_yoy_abs"] == -10.0


def test_compute_year_over_year_change_missing_lookup_entry_gives_none():
    rows = [{"period": "2022-03", "mean_radiance": 12.0}]
    out = compute_year_over_year_change(rows, "monthly", lookup={})
    assert out[0]["mean_radiance_yoy_abs"] is None
    assert out[0]["mean_radiance_yoy_pct"] is None


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
