#!/usr/bin/env python3
"""
nightlight_tool.py — VIIRS nighttime-lights time series for any area of interest.

Given an area of interest (a boundary file, or a country/admin-unit name), a date
range, and a frequency, this pulls VIIRS Day/Night Band radiance from Google Earth
Engine and writes a per-period time series (CSV + a quick chart PNG).

Quick start
-----------
    pip install -r requirements.txt
    earthengine authenticate      # one-time, needs a free Google account
    python nightlight_tool.py --aoi-file sample_aoi/crimea.geojson \\
        --start 2021-01-01 --end 2023-01-01 --freq monthly --out crimea_nightlights.csv

See README.md for full setup and usage details.

Design notes
------------
The "pure" logic (period construction, pixel-array statistics, QA flagging, output
writing) lives in plain functions with no Earth Engine dependency, so it can be
unit-tested offline (see tests/) without an authenticated EE session. Only
`resolve_aoi_geometry`, `fetch_period_stats`, and `main` touch the `ee` module.

Known limitation: the VNP46A2 cloud/quality bit-flag layout used in
`daily_pixel_quality_mask` reflects the product documentation at the time this was
written. NASA/NOAA have changed VIIRS product band layouts before — if daily results
look implausible, check the current VNP46A2 User Guide's QF_Cloud_Mask bit table
against the constants below before trusting the numbers.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Pure logic — no `ee` import here, so this half is unit-testable offline.
# ---------------------------------------------------------------------------

VALID_FREQS = ("daily", "weekly", "monthly", "annual")

# VNP46A2 Mandatory_Quality_Flag values (per the product's QA band):
#   0 = high-quality, persistent nighttime lights, main algorithm
#   1 = other retrieval quality (e.g. lower-quality VIIRS retrieval)
#   2 = poor quality / gap-filled / snow-covered
# We keep 0 and 1 by default, drop 2. See module docstring caveat above.
MANDATORY_QF_KEEP = (0, 1)

# Minimum fraction of AOI pixels that must have passed the quality mask for a
# daily/period value to be trusted without a QA flag.
MIN_VALID_PIXEL_FRACTION = 0.5
MIN_SCENE_COUNT = 1


@dataclass(frozen=True)
class Period:
    label: str          # e.g. "2022-03", "2022", "2022-03-14"
    start: date          # inclusive
    end: date            # exclusive


def _month_add(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def build_periods(start: str, end: str, freq: str) -> list[Period]:
    """Split [start, end) into a list of Periods at the requested frequency.

    `start`/`end` are ISO date strings (YYYY-MM-DD). `end` is exclusive, matching
    Earth Engine's filterDate convention. Raises ValueError on bad input.
    """
    if freq not in VALID_FREQS:
        raise ValueError(f"freq must be one of {VALID_FREQS}, got {freq!r}")

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    if end_d <= start_d:
        raise ValueError(f"--end ({end}) must be after --start ({start})")

    periods: list[Period] = []

    if freq == "daily":
        cur = start_d
        while cur < end_d:
            nxt = cur + timedelta(days=1)
            periods.append(Period(cur.isoformat(), cur, nxt))
            cur = nxt

    elif freq == "monthly":
        cur = date(start_d.year, start_d.month, 1)
        while cur < end_d:
            nxt = _month_add(cur, 1)
            periods.append(Period(f"{cur.year:04d}-{cur.month:02d}", cur, nxt))
            cur = nxt

    elif freq == "weekly":
        # Snap to the Monday of the start date's ISO week, same spirit as
        # monthly snapping to the 1st -- a partial first week is still
        # reported as a full calendar week rather than a ragged stub.
        cur = start_d - timedelta(days=start_d.weekday())
        while cur < end_d:
            nxt = cur + timedelta(days=7)
            iso_year, iso_week, _ = cur.isocalendar()
            periods.append(Period(f"{iso_year:04d}-W{iso_week:02d}", cur, nxt))
            cur = nxt

    elif freq == "annual":
        cur = date(start_d.year, 1, 1)
        while cur < end_d:
            nxt = date(cur.year + 1, 1, 1)
            periods.append(Period(f"{cur.year:04d}", cur, nxt))
            cur = nxt

    return periods


_PERIOD_LABEL_EXAMPLES = {
    "daily": "2021-01-15",
    "weekly": "2021-W05",
    "monthly": "2021-01",
    "annual": "2021",
}


def parse_period_label(label: str, freq: str) -> Period:
    """Pure function: the inverse of build_periods() -- given one period
    label in the same format build_periods() would have produced for
    `freq` (e.g. "2021-01" for monthly, "2021" for annual, "2021-W05" for
    weekly, "2021-01-15" for daily), reconstruct the matching Period
    (start/end dates). Used for --baseline-period, so a baseline period
    doesn't have to be one of the periods --start/--end already covers --
    it's fetched as one extra period in its own right, the same way as any
    other. Raises ValueError on a label that doesn't match `freq`'s format.
    """
    if freq not in VALID_FREQS:
        raise ValueError(f"freq must be one of {VALID_FREQS}, got {freq!r}")
    try:
        if freq == "daily":
            start_d = date.fromisoformat(label)
            return Period(label, start_d, start_d + timedelta(days=1))
        if freq == "monthly":
            year_s, month_s = label.split("-")
            start_d = date(int(year_s), int(month_s), 1)
            return Period(label, start_d, _month_add(start_d, 1))
        if freq == "annual":
            start_d = date(int(label), 1, 1)
            return Period(label, start_d, date(start_d.year + 1, 1, 1))
        # weekly
        year_s, week_s = label.split("-W")
        start_d = date.fromisocalendar(int(year_s), int(week_s), 1)
        return Period(label, start_d, start_d + timedelta(days=7))
    except (ValueError, IndexError) as e:
        raise ValueError(
            f"--baseline-period {label!r} doesn't look like a {freq} period label "
            f"(expected e.g. {_PERIOD_LABEL_EXAMPLES[freq]!r}): {e}"
        ) from e


DEFAULT_CHANGE_STAT_COLS = ("mean_radiance", "sum_radiance", "median_radiance")


def _stat_diff(current, previous):
    """Pure helper: (absolute, percent) change from `previous` to `current`.
    Either value missing -> (None, None). `previous` is zero -> (absolute
    change, None), since percent change from zero is undefined.
    """
    if current is None or previous is None:
        return None, None
    abs_change = current - previous
    pct_change = (abs_change / previous) * 100 if previous != 0 else None
    return abs_change, pct_change


def compute_period_over_period_change(
    rows: list[dict],
    stat_cols: tuple[str, ...] = DEFAULT_CHANGE_STAT_COLS,
    group_key=None,
) -> list[dict]:
    """Pure function (--include-change): return a copy of `rows` with
    "<stat>_change_abs"/"<stat>_change_pct" columns added for each of
    `stat_cols`, comparing each row to the row immediately before it
    *within its own group*, in the order rows already appear (callers
    build rows one period at a time, so a group's rows are already in
    period order).

    `group_key(row) -> hashable` identifies which series a row belongs to
    -- pass `_chart_key` for a --breakdown run (groups by unit), or leave
    as None to treat every row as one series (a whole-AOI run). The first
    row in each group's series has nothing to diff against, so both new
    columns are None there.

    Doesn't touch `ee` -- pure list/dict manipulation -- so it's testable
    offline like every other row-shaping helper here.
    """
    previous_by_group: dict = {}
    out = []
    for row in rows:
        key = group_key(row) if group_key else None
        previous = previous_by_group.get(key)
        new_row = dict(row)
        for stat in stat_cols:
            abs_change, pct_change = _stat_diff(
                row.get(stat), previous.get(stat) if previous else None
            )
            new_row[f"{stat}_change_abs"] = abs_change
            new_row[f"{stat}_change_pct"] = pct_change
        out.append(new_row)
        previous_by_group[key] = row
    return out


def compute_baseline_change(
    rows: list[dict],
    baseline_rows: list[dict],
    stat_cols: tuple[str, ...] = DEFAULT_CHANGE_STAT_COLS,
    group_key=None,
) -> list[dict]:
    """Pure function (--baseline-period): return a copy of `rows` with
    "<stat>_vs_baseline_abs"/"<stat>_vs_baseline_pct" columns added for
    each of `stat_cols`, comparing each row to its group's one row in
    `baseline_rows` (the baseline period, fetched once, the same way as
    every other period -- see fetch_period_stats/fetch_period_breakdown_stats).
    A row whose group has no matching baseline row gets None for both new
    columns (shouldn't normally happen, since the baseline is fetched for
    every group the same way).

    Doesn't touch `ee`, same as compute_period_over_period_change.
    """
    baseline_by_group = {}
    for brow in baseline_rows:
        key = group_key(brow) if group_key else None
        baseline_by_group[key] = brow

    out = []
    for row in rows:
        key = group_key(row) if group_key else None
        baseline_row = baseline_by_group.get(key)
        new_row = dict(row)
        for stat in stat_cols:
            base_val = baseline_row.get(stat) if baseline_row else None
            abs_change, pct_change = _stat_diff(row.get(stat), base_val)
            new_row[f"{stat}_vs_baseline_abs"] = abs_change
            new_row[f"{stat}_vs_baseline_pct"] = pct_change
        out.append(new_row)
    return out


def _get_stat(feature_properties: dict, band: str, stat: str):
    """Look up one reducer output, tolerant of two different Earth Engine naming
    conventions we've observed in practice: `Image.reduceRegion` (single AOI)
    prefixes outputs with the band name (e.g. "avg_rad_mean"), while
    `Image.reduceRegions` (per-feature, used by --breakdown) has returned the
    bare reducer output name (e.g. "mean") with a single selected band. Rather
    than assume one or the other, check both so this survives an EE API/version
    difference either way.
    """
    for key in (stat, f"{band}_{stat}"):
        if key in feature_properties:
            return feature_properties[key]
    return None


def build_breakdown_row(
    period_label: str,
    band: str,
    feature_properties: dict,
    scene_count: int,
    attribute_fields: Optional[list[str]] = None,
) -> dict:
    """Pure function: turn one reduceRegions-output feature's properties into a row.

    `feature_properties` is a plain dict (as returned by ee's getInfo(), or a fake
    one in tests) — this function makes no Earth Engine calls itself, which is what
    keeps it unit-testable without an EE session.

    `attribute_fields`, when given, names the exact properties/columns from the
    admin/boundary data to carry into the output as their own columns (e.g.
    ["ADM0_NAME", "ADM1_NAME"] for GAUL, or user-chosen column names from an
    --aoi-file). This is how the caller picks what ends up in the CSV instead of
    always getting the three hardcoded GAUL admin-name columns below, which stay
    as the default for backward compatibility when no fields are requested.

    `unit_id` (in `feature_properties`) is always surfaced as its own column,
    separate from `attribute_fields` -- it's the stable, unique identifier for
    the unit (a GAUL ADM*_CODE, or whichever column the user pointed at as a
    unique key, e.g. a pcode) as opposed to `unit_name`, which is
    human-readable but not guaranteed unique (two municipios can share a
    name). It's None when the caller didn't have one to set.
    """
    row: dict = {
        "period": period_label,
        "unit_name": feature_properties.get("unit_name"),
        "unit_id": feature_properties.get("unit_id"),
    }
    if attribute_fields:
        for field in attribute_fields:
            row[field] = feature_properties.get(field)
    else:
        row["admin0_name"] = feature_properties.get("ADM0_NAME")
        row["admin1_name"] = feature_properties.get("ADM1_NAME")
        row["admin2_name"] = feature_properties.get("ADM2_NAME")
    row.update(
        {
            "mean_radiance": _get_stat(feature_properties, band, "mean"),
            "sum_radiance": _get_stat(feature_properties, band, "sum"),
            "median_radiance": _get_stat(feature_properties, band, "median"),
            "valid_pixel_count": _get_stat(feature_properties, band, "count"),
            "scene_count": scene_count,
            "qa_flag": qa_flag(scene_count, None),
        }
    )
    return row


def qa_flag(scene_count: int, valid_pixel_fraction: Optional[float]) -> str:
    """Pure QA classification, independent of how the counts were produced.

    `valid_pixel_fraction` is None when it isn't tracked for this product path
    (e.g. the pre-composited monthly product has no per-pixel quality band).
    """
    if scene_count < MIN_SCENE_COUNT:
        return "no_data"
    if valid_pixel_fraction is not None and valid_pixel_fraction < MIN_VALID_PIXEL_FRACTION:
        return "low_valid_pixels"
    return "ok"


def summarize_pixels(values, mask) -> dict:
    """Zonal-style stats over a 2D array of radiance values and a boolean mask.

    Pure numpy — used both by the offline smoke tests and as a stand-in for what
    ee.Reducer does server-side, so the two can be reasoned about the same way.
    """
    import numpy as np

    values = np.asarray(values, dtype=float)
    mask = np.asarray(mask, dtype=bool)
    total_pixels = int(values.size)
    valid = values[mask]
    valid_pixel_count = int(valid.size)
    valid_fraction = (valid_pixel_count / total_pixels) if total_pixels else 0.0

    if valid_pixel_count == 0:
        return {
            "mean_radiance": None,
            "sum_radiance": None,
            "median_radiance": None,
            "valid_pixel_count": 0,
            "total_pixel_count": total_pixels,
            "valid_pixel_fraction": 0.0,
        }

    return {
        "mean_radiance": float(np.mean(valid)),
        "sum_radiance": float(np.sum(valid)),
        "median_radiance": float(np.median(valid)),
        "valid_pixel_count": valid_pixel_count,
        "total_pixel_count": total_pixels,
        "valid_pixel_fraction": valid_fraction,
    }


def write_csv(rows: Iterable[dict], out_path: Path, fieldnames: Optional[list[str]] = None) -> None:
    rows = list(rows)
    if not rows:
        raise ValueError("no rows to write")
    if fieldnames is None:
        fieldnames = list(rows[0].keys())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


DEFAULT_MAX_BREAKDOWN_CHART_PANELS = 30


def _chart_key(row: dict):
    """Grouping key for one row's unit in a --breakdown chart.

    unit_name alone isn't a safe key -- it's only meant to be human-readable
    and two different units (e.g. two admin3 units in different admin2s)
    can share a name. When the row carries a unit_id, that's used instead;
    only rows with no unit_id at all (older runs, or --aoi-file without
    --unit-id-field) fall back to grouping by unit_name as before.
    """
    uid = row.get("unit_id")
    if uid not in (None, ""):
        return uid
    return row.get("unit_name")


def select_breakdown_chart_units(
    rows: list[dict],
    unit_filter: Optional[list[str]] = None,
    max_panels: int = DEFAULT_MAX_BREAKDOWN_CHART_PANELS,
) -> list:
    """Pure function: decide which units a --breakdown chart should draw one
    panel for, and in what order. Returns a list of chart keys (see
    _chart_key) -- plain unit_name strings when no unit_id is present, so
    this is a drop-in superset of the old name-only behaviour.

    With `unit_filter` given (exact unit_name values, as the user typed them),
    returns every distinct unit belonging to a matching name, grouped by name
    in the order given -- if that name turns out to belong to more than one
    unit (names collide), all of them are included rather than merged into
    one panel -- deliberate selection needs no cap. Without a filter, returns
    every unit that appears in `rows`, in first-seen order, unless that's
    more than `max_panels`, in which case it truncates and the caller is
    expected to warn (a chart with hundreds of tiny panels isn't useful, and
    the fix is to either narrow with --chart-units or just pivot the CSV
    yourself for a full per-unit view).
    """
    keys_by_name: dict[str, list] = {}
    seen_keys = set()
    order: list = []
    for row in rows:
        key = _chart_key(row)
        if key is None:
            continue
        if key not in seen_keys:
            seen_keys.add(key)
            order.append(key)
            keys_by_name.setdefault(row.get("unit_name"), []).append(key)

    if unit_filter:
        result = []
        for name in unit_filter:
            result.extend(keys_by_name.get(name, []))
        return result

    return order[:max_panels]


def write_breakdown_chart(
    rows: list[dict],
    chart_path: Path,
    units: list,
    value_field: str = "mean_radiance",
) -> None:
    """Write a small-multiples PNG: one mini line chart per unit in `units`,
    each showing `value_field` over `period`. Companion to write_chart() for
    --breakdown output, where a single shared chart with one line per unit
    isn't legible once there are more than a handful of units.

    `units` is a list of chart keys as returned by select_breakdown_chart_units
    (unit_id when available, else unit_name) -- rows are grouped by that same
    key so units sharing a name never get merged into one panel.
    """
    import math

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not units:
        raise ValueError("no units to chart")

    by_unit: dict = {u: [] for u in units}
    unit_titles: dict = {}
    for row in rows:
        key = _chart_key(row)
        if key in by_unit:
            by_unit[key].append(row)
            if key not in unit_titles:
                name = row.get("unit_name")
                uid = row.get("unit_id")
                unit_titles[key] = f"{name} ({uid})" if uid not in (None, "") else str(name)

    n = len(units)
    ncols = min(4, n)
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.0 * nrows), squeeze=False)

    for i, unit in enumerate(units):
        ax = axes[i // ncols][i % ncols]
        unit_rows = by_unit[unit]
        labels = [r["period"] for r in unit_rows]
        values = [r.get(value_field) for r in unit_rows]
        ax.plot(labels, values, marker="o", linewidth=1.2, markersize=3)
        ax.set_title(unit_titles.get(unit, str(unit)), fontsize=9)
        ax.tick_params(axis="x", rotation=60, labelsize=7)
        ax.tick_params(axis="y", labelsize=7)
        for flag_row in unit_rows:
            if flag_row.get("qa_flag") not in (None, "ok"):
                ax.axvspan(flag_row["period"], flag_row["period"], color="red", alpha=0.15)

    # Hide any unused grid cells (n doesn't always divide evenly into the grid).
    for i in range(n, nrows * ncols):
        axes[i // ncols][i % ncols].axis("off")

    fig.suptitle(f"VIIRS nighttime-lights radiance by unit ({value_field.replace('_', ' ')})")
    fig.tight_layout()
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(chart_path, dpi=150)
    plt.close(fig)


def write_chart(rows: list[dict], chart_path: Path, value_field: str = "mean_radiance") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [r["period"] for r in rows]
    values = [r.get(value_field) for r in rows]

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(labels, values, marker="o", linewidth=1.5)
    ax.set_ylabel(value_field.replace("_", " "))
    ax.set_title("VIIRS nighttime-lights radiance")
    ax.tick_params(axis="x", rotation=60)
    for flag_row in rows:
        if flag_row.get("qa_flag") not in (None, "ok"):
            ax.axvspan(flag_row["period"], flag_row["period"], color="red", alpha=0.15)
    fig.tight_layout()
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(chart_path, dpi=150)
    plt.close(fig)


def resolve_iso3_candidate_names(iso3: str) -> list[str]:
    """Pure function: given an ISO 3166-1 alpha-3 code, return candidate country
    name strings to try against FAO GAUL's ADM0_NAME field.

    GAUL boundaries don't carry ISO codes themselves, and pycountry's own name
    fields often don't match GAUL's exact ADM0_NAME string either (e.g. for
    COD, pycountry's `name` is "Congo, The Democratic Republic of the" while
    GAUL uses "Democratic Republic of the Congo") -- so this returns several
    candidates (common/short name, full name, official name) for the caller
    to try in turn, with a final fallback match against the live GAUL country
    list happening on the Earth Engine side (see resolve_iso3_to_gaul_name).

    Raises ValueError for a code pycountry doesn't recognise. No `ee` import
    here, so this half is unit-testable offline like the rest of this section.
    """
    import pycountry

    country = pycountry.countries.get(alpha_3=iso3.upper())
    if country is None:
        raise ValueError(
            f"{iso3!r} is not a recognised ISO 3166-1 alpha-3 country code "
            "(e.g. 'UKR', 'USA', 'KOR')."
        )
    candidates = []
    for attr in ("common_name", "name", "official_name"):
        val = getattr(country, attr, None)
        if val and val not in candidates:
            candidates.append(val)
    return candidates


# ---------------------------------------------------------------------------
# Earth Engine glue — only this half touches `ee`.
# ---------------------------------------------------------------------------

def _ensure_ee_initialized(ee_project: Optional[str] = None) -> None:
    """Initialize Earth Engine if it isn't already, used by the wizard's live
    lookups which can run before main()'s own ee.Initialize() call."""
    import ee

    try:
        ee.data.getAssetRoots()
    except Exception:  # noqa: BLE001 -- not yet initialized
        if ee_project:
            ee.Initialize(project=ee_project)
        else:
            ee.Initialize()


def resolve_iso3_to_gaul_name(iso3: str, ee_project: Optional[str] = None) -> str:
    """Match an ISO3 code to the exact ADM0_NAME string FAO GAUL uses for that
    country. Tries resolve_iso3_candidate_names()'s candidates as exact
    matches first, then falls back to a case-insensitive match against GAUL's
    actual country list (GAUL level0 is only ~250 features, small enough to
    pull client-side for this). Raises ValueError, listing what was tried,
    if nothing lines up -- at that point --aoi-name with the exact GAUL name
    is the fallback.
    """
    import ee

    _ensure_ee_initialized(ee_project)
    candidates = resolve_iso3_candidate_names(iso3)
    gaul0 = ee.FeatureCollection("FAO/GAUL/2015/level0")

    for name in candidates:
        if gaul0.filter(ee.Filter.eq("ADM0_NAME", name)).size().getInfo() > 0:
            return name

    all_names = gaul0.aggregate_array("ADM0_NAME").getInfo()
    lower_map = {n.lower(): n for n in all_names}
    for name in candidates:
        hit = lower_map.get(name.lower())
        if hit:
            return hit

    for name in candidates:
        substring_matches = [
            n for n in all_names if name.lower() in n.lower() or n.lower() in name.lower()
        ]
        if len(substring_matches) == 1:
            return substring_matches[0]

    raise ValueError(
        f"Could not match ISO3 {iso3!r} to a FAO GAUL country name. Tried: "
        f"{candidates}. Use --aoi-name with the exact GAUL ADM0_NAME instead "
        "-- inspect FAO/GAUL/2015/level0's ADM0_NAME values if you're not "
        "sure what GAUL calls it."
    )


def resolve_aoi_geometry(aoi_file: Optional[str], aoi_name: Optional[str]):
    """Return an ee.Geometry for the AOI, from a boundary file or a name lookup."""
    import ee

    if aoi_file:
        import geopandas as gpd

        gdf = gpd.read_file(aoi_file)
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        # Dissolve to a single geometry so multi-feature files (e.g. several
        # raions) are treated as one AOI, matching the --aoi-name behaviour.
        geom = gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union
        return ee.Geometry(geom.__geo_interface__)

    if aoi_name:
        # Try country level first (GAUL level 0), then admin-1, then admin-2.
        candidates = [
            ("FAO/GAUL/2015/level0", "ADM0_NAME"),
            ("FAO/GAUL/2015/level1", "ADM1_NAME"),
            ("FAO/GAUL/2015/level2", "ADM2_NAME"),
        ]
        for asset_id, name_field in candidates:
            fc = ee.FeatureCollection(asset_id).filter(
                ee.Filter.eq(name_field, aoi_name)
            )
            if fc.size().getInfo() > 0:
                return fc.geometry()
        raise ValueError(
            f"No GAUL admin boundary matched {aoi_name!r} at country, admin-1, "
            "or admin-2 level. Check spelling/capitalisation, or supply "
            "--aoi-file instead."
        )

    raise ValueError("Must supply either --aoi-file or --aoi-name")


def daily_pixel_quality_mask(image):
    """Mask a VNP46A2 image to reasonably reliable pixels only.

    See the module docstring: verify MANDATORY_QF_KEEP against the current
    VNP46A2 product documentation if results look off.
    """
    qf = image.select("Mandatory_Quality_Flag")
    keep_mask = qf.eq(MANDATORY_QF_KEEP[0])
    for v in MANDATORY_QF_KEEP[1:]:
        keep_mask = keep_mask.Or(qf.eq(v))
    return image.updateMask(keep_mask)


def _get_period_image_and_scene_count(freq: str, period: Period):
    """Shared by both single-AOI and breakdown paths: resolve the (image, band,
    scene_count) for one period, or (None, band, 0) if nothing's available.
    """
    import ee

    start_str = period.start.isoformat()
    end_str = period.end.isoformat()

    if freq in ("monthly", "annual"):
        band = "avg_rad"
        coll = (
            ee.ImageCollection("NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG")
            .filterDate(start_str, end_str)
            .select(band)
        )
        scene_count = coll.size().getInfo()
        if scene_count == 0:
            return None, band, 0
        # Annual = mean-composite the monthly images first, then reduce once.
        image = coll.mean() if freq == "annual" else coll.mosaic()
        return image, band, scene_count

    # daily or weekly: both mosaic the gap-filled daily product over the
    # period's date range (a single day for "daily", a calendar week for
    # "weekly") -- there's no native VIIRS weekly composite product, so a
    # week is built the same way "annual" is built from monthly images: by
    # combining the finer-grained product over a wider window.
    band = "Gap_Filled_DNB_BRDF_Corrected_NTL"
    coll = (
        ee.ImageCollection("NASA/VIIRS/002/VNP46A2")
        .filterDate(start_str, end_str)
        .map(daily_pixel_quality_mask)
        .select(band)
    )
    scene_count = coll.size().getInfo()
    if scene_count == 0:
        return None, band, 0
    image = coll.mosaic()
    return image, band, scene_count


def _combined_reducer():
    import ee

    return (
        ee.Reducer.mean()
        .combine(ee.Reducer.sum(), sharedInputs=True)
        .combine(ee.Reducer.median(), sharedInputs=True)
        .combine(ee.Reducer.count(), sharedInputs=True)
    )


def fetch_period_stats(freq: str, aoi_geom, period: Period, scale: int = 500) -> dict:
    """Query Earth Engine for one period's zonal radiance stats + QA fields
    over a single AOI geometry."""
    image, band, scene_count = _get_period_image_and_scene_count(freq, period)
    if image is None:
        return {
            "period": period.label,
            "mean_radiance": None,
            "sum_radiance": None,
            "median_radiance": None,
            "valid_pixel_count": None,
            "total_pixel_count": None,
            "scene_count": 0,
            "qa_flag": "no_data",
        }

    stats = image.reduceRegion(
        reducer=_combined_reducer(), geometry=aoi_geom, scale=scale, maxPixels=1e10, bestEffort=True
    ).getInfo()
    return {
        "period": period.label,
        "mean_radiance": stats.get(f"{band}_mean"),
        "sum_radiance": stats.get(f"{band}_sum"),
        "median_radiance": stats.get(f"{band}_median"),
        "valid_pixel_count": stats.get(f"{band}_count"),
        "total_pixel_count": None,  # not tracked for the pre-composited/mosaicked product
        "scene_count": scene_count,
        "qa_flag": qa_flag(scene_count, None),
    }


def simplify_geometry(geom, tolerance: Optional[float]):
    """Pure geometry step: simplify a shapely geometry if a tolerance is given,
    otherwise return it unchanged. Kept separate from the ee-glue code below so
    it's unit-testable without an Earth Engine session.

    Earth Engine rejects a client-side literal (an ee.FeatureCollection built
    from local geometries, as --aoi-file + --breakdown does) once its payload
    exceeds 10MB — every polygon's full vertex list gets embedded in every API
    call that touches it. A detailed admin2/raion layer with a few hundred
    units can blow past that easily. Since VIIRS itself only resolves to
    ~500m, simplifying well below that scale won't meaningfully change zonal
    stats for reasonably-sized units — but very small or thin units could be
    affected, so this is opt-in via --simplify-tolerance, not automatic.
    """
    if tolerance is None or tolerance <= 0:
        return geom
    return geom.simplify(tolerance, preserve_topology=True)


GAUL_BREAKDOWN_LEVELS: dict[str, tuple[str, str, str]] = {
    "admin1": ("FAO/GAUL/2015/level1", "ADM1_NAME", "ADM1_CODE"),
    "admin2": ("FAO/GAUL/2015/level2", "ADM2_NAME", "ADM2_CODE"),
}


def _unsupported_gaul_level_error(breakdown: str) -> ValueError:
    return ValueError(
        f"--breakdown {breakdown} isn't available with --aoi-name/--aoi-iso3 -- "
        "FAO GAUL 2015 only carries admin1 and admin2 below the country level. "
        "For finer units (admin3+), supply your own boundary file with --aoi-file "
        "instead (the admin level there is just a label for the output)."
    )


def gaul_unit_name_id_fields(breakdown: str) -> tuple[str, str]:
    """Return (name_field, id_field) -- the GAUL property names that back
    unit_name/unit_id for a given --breakdown level with --aoi-name/--aoi-iso3
    (e.g. ("ADM1_NAME", "ADM1_CODE") for admin1).

    This is the same lookup resolve_breakdown_collection() uses internally to
    build the feature collection, exposed separately so main() can also use
    it -- to name the output CSV's unit_name/unit_id columns after whichever
    field actually produced them (see rename_unit_columns()) -- without a
    second, drifting copy of the admin-level table.
    """
    if breakdown not in GAUL_BREAKDOWN_LEVELS:
        raise _unsupported_gaul_level_error(breakdown)
    _, name_field, id_field = GAUL_BREAKDOWN_LEVELS[breakdown]
    return name_field, id_field


def rename_unit_columns(
    rows: list[dict], unit_name_column: str, unit_id_column: Optional[str]
) -> list[dict]:
    """Pure function: rename the generic 'unit_name'/'unit_id' keys in each
    row to whichever column actually identifies each unit -- e.g. 'adm2_name'
    and 'adm2_pcode' for a --unit-name-field/--unit-id-field pair, or
    'ADM1_NAME'/'ADM1_CODE' for a GAUL admin1 breakdown -- so the CSV header
    says what the values actually are instead of a generic label.

    This is a presentation-only rename applied right before the CSV is
    written. Every other part of the tool (charting, --chart-units matching,
    the wizard/GUI, the rest of build_breakdown_row/fetch_period_breakdown_stats)
    keeps using the generic 'unit_name'/'unit_id' keys internally -- rows
    passed in here are a fresh set of dicts, the originals are untouched, so
    callers that still need the generic keys (e.g. for charting after this
    is called) keep working from the pre-rename rows.

    unit_id_column of None (or already 'unit_id') leaves the 'unit_id' key
    as-is -- there's no more specific name to rename it to when no
    --unit-id-field was given. Key order is preserved, since csv.DictWriter
    takes its header from the first row's key order.
    """
    if not rows:
        return rows
    if unit_name_column == "unit_name" and unit_id_column in (None, "unit_id"):
        return rows  # nothing to rename -- avoid needless copies

    renamed = []
    for row in rows:
        new_row = {}
        for key, value in row.items():
            if key == "unit_name" and unit_name_column != "unit_name":
                new_row[unit_name_column] = value
            elif key == "unit_id" and unit_id_column and unit_id_column != "unit_id":
                new_row[unit_id_column] = value
            else:
                new_row[key] = value
        renamed.append(new_row)
    return renamed


def attach_geometry(rows: list[dict], geometry_for_row) -> list[dict]:
    """Pure function: return a copy of `rows` with a "geometry" key added to
    each, via `geometry_for_row(row)` -- a plain callable, not an Earth
    Engine object, so this takes no `ee` dependency and is unit-testable
    with a fake lookup. A row whose geometry can't be found gets
    geometry=None (the caller decides whether/how to warn and drop those).
    """
    out = []
    for row in rows:
        new_row = dict(row)
        new_row["geometry"] = geometry_for_row(row)
        out.append(new_row)
    return out


def shapefile_safe_field_names(names: list[str]) -> dict[str, str]:
    """Pure function: map each name to a version that fits the ESRI
    Shapefile format's 10-character field-name limit, keeping every result
    unique. Longer names are truncated to 10 characters; if that collides
    with an already-assigned name, characters are dropped off the end to
    make room for a disambiguating digit (or digits, if even more collide).

    geopandas/fiona will silently truncate+may-collide on their own when
    writing a shapefile with long field names -- doing it explicitly here
    means collisions are visible (as a warning at the call site) instead of
    quietly dropping/overwriting a column.
    """
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for name in names:
        candidate = name[:10]
        suffix_n = 1
        while candidate in used:
            suffix = str(suffix_n)
            candidate = name[: 10 - len(suffix)] + suffix
            suffix_n += 1
        used.add(candidate)
        mapping[name] = candidate
    return mapping


def _geo_out_driver(out_path: Path) -> str:
    """Pure function: infer the geopandas driver name from a --geo-out-style
    path's extension, or raise ValueError with a message naming the bad
    path -- shared by every --geo-out write path so the accepted-extensions
    rule only lives in one place.
    """
    suffix = out_path.suffix.lower()
    if suffix in (".geojson", ".json"):
        return "GeoJSON"
    if suffix == ".shp":
        return "ESRI Shapefile"
    raise ValueError(
        f"--geo-out path must end in .geojson or .shp, got {out_path.suffix!r} ({out_path})"
    )


def _write_geo_file(rows: list[dict], out_path: Path, driver: str):
    """Write one GeoDataFrame built from `rows` (each carrying a "geometry"
    key, see attach_geometry) to `out_path` in `driver` format, applying the
    shapefile field-name-truncation safety net when writing a Shapefile.
    Used by write_geo_outputs_per_period for every period's file, so the
    truncation/warning behavior is consistent across all of them.
    """
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    if driver == "ESRI Shapefile":
        mapping = shapefile_safe_field_names([c for c in gdf.columns if c != "geometry"])
        renamed = {k: v for k, v in mapping.items() if k != v}
        if renamed:
            print(
                f"Note: shortened {len(renamed)} field name(s) to fit the shapefile "
                f"10-character limit: {renamed}",
                file=sys.stderr,
            )
        gdf.rename(columns=mapping, inplace=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, driver=driver)


def split_rows_by_period(rows: list[dict], period_col: str = "period") -> dict[str, list[dict]]:
    """Pure function: group already-geometry-attached rows (see
    attach_geometry) by their period value, preserving first-seen period
    order. This is how --geo-out builds one spatial file per period -- one
    per frequency step (month/week/year/day, whichever --freq is), matching
    what --breakdown/--freq already computed, rather than one combined
    multi-period file.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        period = row.get(period_col)
        groups.setdefault(period, []).append(row)
    return groups


def write_geo_outputs_per_period(
    rows_by_period: dict[str, list[dict]],
    out_path: Path,
    drop_cols: tuple[str, ...] = ("period",),
) -> dict[str, Path]:
    """Write one spatial file per period from `rows_by_period` (see
    split_rows_by_period), named "<stem>_<period><ext>" next to `out_path"
    -- the period is already at the end of every filename, so `drop_cols`
    defaults to dropping the now-redundant "period" column from each
    feature's attributes (every row in a given file already shares that one
    period). Format is inferred from `out_path`'s extension. Returns
    {period: path_written}, in the same order as `rows_by_period`.

    Doesn't touch `ee` -- only geopandas file I/O -- so it's testable
    offline by writing to a temp path and reading the result back.
    """
    driver = _geo_out_driver(out_path)
    written: dict[str, Path] = {}
    for period, rows in rows_by_period.items():
        period_path = out_path.with_name(f"{out_path.stem}_{period}{out_path.suffix}")
        trimmed_rows = [{k: v for k, v in row.items() if k not in drop_cols} for row in rows]
        _write_geo_file(trimmed_rows, period_path, driver)
        written[period] = period_path
    return written


def write_geo_outputs_combined(rows: list[dict], out_path: Path) -> Path:
    """Write ONE spatial file containing every row across every period --
    the "one to many" join Matt asked for: one feature per (unit, period)
    pair, geometry repeated per period, so it has the same row shape as the
    CSV. Unlike write_geo_outputs_per_period, the "period" column is kept
    (it's the only thing distinguishing rows for the same unit here, since
    there's no per-period filename to carry that instead). Named
    "<stem>_all_periods<ext>" next to `out_path`, alongside the per-period
    files write_geo_outputs_per_period writes to that same `out_path`.

    Doesn't touch `ee` -- only geopandas file I/O -- so it's testable
    offline the same way write_geo_outputs_per_period is.
    """
    driver = _geo_out_driver(out_path)
    combined_path = out_path.with_name(f"{out_path.stem}_all_periods{out_path.suffix}")
    _write_geo_file(rows, combined_path, driver)
    return combined_path


def resolve_breakdown_collection(
    aoi_file: Optional[str],
    aoi_name: Optional[str],
    breakdown: str,
    unit_name_field: Optional[str],
    simplify_tolerance: Optional[float] = None,
    attribute_fields: Optional[list[str]] = None,
    unit_id_field: Optional[str] = None,
):
    """Return an ee.FeatureCollection of sub-units to break the analysis down by,
    each carrying a 'unit_name' property.

    --aoi-name + --breakdown: looks up FAO GAUL admin1/admin2 units within that
    country. --aoi-file + --breakdown: keeps every feature in the file separate
    (rather than dissolving them, like the single-AOI path does) and labels each
    from --unit-name-field.

    `attribute_fields`, when given, are extra property/column names to carry
    through onto each unit so they end up as columns in the output (see
    build_breakdown_row). GAUL features already carry their admin-name/code
    properties natively, so nothing extra is needed there; for --aoi-file the
    requested columns are read from the boundary file and validated here, the
    same way --unit-name-field already is.

    Every unit also gets a 'unit_id' property -- a stable, unique identifier,
    as opposed to 'unit_name' which is only meant to be human-readable and can
    collide (two municipios sharing a name in different states, say). For
    --aoi-name this is filled in automatically from GAUL's own ADM1_CODE/
    ADM2_CODE, since those always exist. For --aoi-file it comes from
    `unit_id_field` -- typically a pcode column -- which is optional but
    strongly recommended whenever unit names might not be unique; when it's
    not given, 'unit_id' is left None for every unit.
    """
    import ee

    if aoi_name:
        if breakdown not in GAUL_BREAKDOWN_LEVELS:
            raise _unsupported_gaul_level_error(breakdown)
        asset_id, name_field, id_field = GAUL_BREAKDOWN_LEVELS[breakdown]
        fc = ee.FeatureCollection(asset_id).filter(ee.Filter.eq("ADM0_NAME", aoi_name))
        count = fc.size().getInfo()
        if count == 0:
            raise ValueError(
                f"No {breakdown} units found for country name {aoi_name!r} in "
                f"{asset_id}. GAUL's country naming can differ from common usage "
                "— check spelling/capitalisation."
            )
        return fc.map(
            lambda f: f.set("unit_name", f.get(name_field)).set("unit_id", f.get(id_field))
        )

    if aoi_file:
        if not unit_name_field:
            raise ValueError(
                "--unit-name-field is required when using --breakdown with --aoi-file "
                "(it names the column/property in your boundary file to label each unit with)"
            )
        import geopandas as gpd

        gdf = gpd.read_file(aoi_file)
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        if unit_name_field not in gdf.columns:
            raise ValueError(
                f"{unit_name_field!r} not found in {aoi_file} columns: {list(gdf.columns)}"
            )
        if unit_id_field and unit_id_field not in gdf.columns:
            raise ValueError(
                f"--unit-id-field {unit_id_field!r} not found in {aoi_file} columns: "
                f"{list(gdf.columns)}"
            )
        if attribute_fields:
            missing = [f for f in attribute_fields if f not in gdf.columns]
            if missing:
                raise ValueError(
                    f"--attributes field(s) {missing} not found in {aoi_file} columns: "
                    f"{list(gdf.columns)}"
                )
        features = []
        for _, row in gdf.iterrows():
            properties = {
                "unit_name": row[unit_name_field],
                "unit_id": row[unit_id_field] if unit_id_field else None,
            }
            if attribute_fields:
                for field in attribute_fields:
                    properties[field] = row[field]
            features.append(
                ee.Feature(
                    ee.Geometry(
                        simplify_geometry(row.geometry, simplify_tolerance).__geo_interface__
                    ),
                    properties,
                )
            )
        return ee.FeatureCollection(features)

    raise ValueError("--breakdown needs either --aoi-name or --aoi-file (+ --unit-name-field)")


def fetch_period_breakdown_stats(
    freq: str,
    fc,
    period: Period,
    scale: int = 500,
    attribute_fields: Optional[list[str]] = None,
) -> list[dict]:
    """Query Earth Engine for one period's zonal stats across every unit in `fc`
    in a single reduceRegions call, rather than one call per unit."""
    image, band, scene_count = _get_period_image_and_scene_count(freq, period)

    if image is None:
        unit_names = fc.aggregate_array("unit_name").getInfo()
        try:
            unit_ids = fc.aggregate_array("unit_id").getInfo()
        except Exception:  # noqa: BLE001 -- older collections may not carry unit_id
            unit_ids = []
        if len(unit_ids) != len(unit_names):
            unit_ids = [None] * len(unit_names)

        # Attribute columns (e.g. admin0_name, admin0_pcode) live on the same
        # features -- without pulling them here too, any period that falls into
        # this no-image branch (most often the most recent month, before that
        # month's VIIRS composite has been published) would silently come back
        # with every --attributes column blank instead of just the stats.
        attribute_values: dict[str, list] = {}
        for field in attribute_fields or []:
            try:
                values = fc.aggregate_array(field).getInfo()
            except Exception:  # noqa: BLE001 -- field may not exist on this collection
                values = []
            if len(values) != len(unit_names):
                values = [None] * len(unit_names)
            attribute_values[field] = values

        rows = []
        for i, (name, uid) in enumerate(zip(unit_names, unit_ids)):
            feature_properties = {"unit_name": name, "unit_id": uid}
            for field, values in attribute_values.items():
                feature_properties[field] = values[i]
            rows.append(
                build_breakdown_row(
                    period.label,
                    band,
                    feature_properties,
                    scene_count=0,
                    attribute_fields=attribute_fields,
                )
            )
        return rows

    reduced = image.select(band).reduceRegions(
        collection=fc, reducer=_combined_reducer(), scale=scale, tileScale=4
    )
    features = reduced.getInfo()["features"]
    return [
        build_breakdown_row(
            period.label, band, f["properties"], scene_count, attribute_fields=attribute_fields
        )
        for f in features
    ]


def fetch_unit_geometries(fc) -> list[dict]:
    """For --geo-out: pull each --breakdown unit's geometry (plus its raw
    unit_name/unit_id) from Earth Engine, in one call -- separate from the
    per-period stats query, since geometry doesn't change across periods.

    Returns plain dicts keyed like a raw (pre-rename_unit_columns) row --
    "unit_name", "unit_id", "geometry" (a shapely geometry) -- so
    _chart_key() can be reused as-is to build the join-key lookup main()
    uses to attach each geometry to its matching CSV rows.
    """
    from shapely.geometry import shape

    features = fc.getInfo()["features"]
    return [
        {
            "unit_name": f.get("properties", {}).get("unit_name"),
            "unit_id": f.get("properties", {}).get("unit_id"),
            "geometry": shape(f["geometry"]),
        }
        for f in features
    ]


def fetch_whole_aoi_geometry(aoi_geom):
    """For --geo-out on a non-breakdown run: pull the single whole-AOI
    geometry down from Earth Engine as a shapely geometry."""
    from shapely.geometry import shape

    return shape(aoi_geom.getInfo())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract a VIIRS nighttime-lights radiance time series for an AOI."
    )
    aoi_group = p.add_mutually_exclusive_group(required=True)
    aoi_group.add_argument("--aoi-file", help="Path to a boundary file (GeoJSON/shapefile/etc.)")
    aoi_group.add_argument("--aoi-name", help="Country/admin name to look up in FAO GAUL")
    aoi_group.add_argument(
        "--aoi-iso3",
        help=(
            "ISO 3166-1 alpha-3 country code to look up (e.g. 'UKR'). Matched against "
            "FAO GAUL's country names automatically (GAUL doesn't carry ISO codes itself) "
            "-- if no confident match is found, use --aoi-name with the exact GAUL name."
        ),
    )
    p.add_argument("--start", required=True, help="Start date, YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="End date, YYYY-MM-DD (exclusive)")
    p.add_argument("--freq", required=True, choices=VALID_FREQS, help="Time step")
    p.add_argument("--out", required=True, help="Output CSV path")
    p.add_argument(
        "--geo-out",
        default=None,
        help=(
            "Optional path for spatial output joined to the boundary geometry by the unit's "
            "unique identifier/pcode, in addition to --out's CSV. Format is inferred from the "
            "extension: .geojson or .shp. Writes one file per period (month/week/year/day, "
            "whichever --freq is), named '<stem>_<period><ext>' next to this path -- one "
            "feature per unit, with the unit's name/ID/attributes columns plus mean_radiance, "
            "sum_radiance, median_radiance, valid_pixel_count, scene_count, and qa_flag -- plus "
            "one combined file with every period in it (one feature per unit per period, same "
            "row shape as the CSV, geometry repeated per period), named '<stem>_all_periods<ext>'. "
            "Works with or without --breakdown -- without it, there's just one implicit 'unit' "
            "(the whole AOI)."
        ),
    )
    p.add_argument(
        "--include-change",
        action="store_true",
        help=(
            "Add <stat>_change_abs/<stat>_change_pct columns (for mean_radiance, "
            "sum_radiance, median_radiance) to --out's CSV (and --geo-out, if given), "
            "comparing each row to the immediately previous period in its series -- "
            "blank for the first period, since there's nothing before it. With "
            "--breakdown, 'series' means per unit; without it, the whole AOI's one "
            "time series."
        ),
    )
    p.add_argument(
        "--baseline-period",
        default=None,
        help=(
            "Add <stat>_vs_baseline_abs/<stat>_vs_baseline_pct columns comparing every "
            "row to one fixed reference period (e.g. a pre-war baseline), to --out's "
            "CSV (and --geo-out, if given). Give it as a period label matching --freq's "
            "format: '2021-01-15' for daily, '2021-W05' for weekly, '2021-01' for "
            "monthly, '2021' for annual. Doesn't have to fall inside --start/--end -- "
            "it's fetched as one extra period."
        ),
    )
    p.add_argument(
        "--chart",
        action="store_true",
        help=(
            "Also write a PNG chart next to the CSV. Without --breakdown, one line "
            "chart for the whole AOI. With --breakdown, a small-multiples grid with "
            "one mini chart per unit (see --chart-units to limit which ones)."
        ),
    )
    p.add_argument(
        "--chart-units",
        default=None,
        help=(
            "Comma-separated exact unit_name values to chart, when using --chart with "
            "--breakdown. If omitted, every unit is charted, up to "
            f"{DEFAULT_MAX_BREAKDOWN_CHART_PANELS} -- beyond that the chart is truncated "
            "with a warning, since a grid of hundreds of tiny panels isn't useful."
        ),
    )
    p.add_argument(
        "--ee-project",
        default=None,
        help="Earth Engine cloud project ID, if your account needs one (see README)",
    )
    p.add_argument(
        "--breakdown",
        choices=("admin1", "admin2", "admin3", "admin4", "admin5"),
        default=None,
        help=(
            "Instead of one AOI-wide row per period, output one row per admin unit "
            "per period. With --aoi-name, looks up FAO GAUL admin1/admin2 units "
            "within that country -- admin3-5 aren't available from GAUL (it only "
            "carries levels 0-2), so those choices only work with --aoi-file. With "
            "--aoi-file, keeps each feature in the file separate (needs "
            "--unit-name-field) and the admin level is just a label for the output."
        ),
    )
    p.add_argument(
        "--unit-name-field",
        default=None,
        help="Property/column in --aoi-file to label each unit with, when using --breakdown with --aoi-file",
    )
    p.add_argument(
        "--unit-id-field",
        default=None,
        help=(
            "Property/column in --aoi-file that uniquely identifies each unit (typically a "
            "pcode), when using --breakdown with --aoi-file. Included as its own 'unit_id' "
            "column in the output -- unlike --unit-name-field, which is only guaranteed to be "
            "human-readable, not unique (two municipios can share a name). With --aoi-name, "
            "unit_id is filled in automatically from FAO GAUL's ADM1_CODE/ADM2_CODE, so this "
            "flag isn't needed there."
        ),
    )
    p.add_argument(
        "--attributes",
        default=None,
        help=(
            "Comma-separated list of extra property/column names from the admin data to "
            "include as their own columns in --breakdown output. With --aoi-name these are "
            "GAUL property names (e.g. ADM0_NAME,ADM1_NAME,ADM0_CODE); with --aoi-file these "
            "are column names from your boundary file. If omitted, --aoi-name output defaults "
            "to admin0_name/admin1_name/admin2_name and --aoi-file output has no extra columns "
            "beyond unit_name -- use this to disambiguate units that share a name (e.g. two "
            "municipios called the same thing in different states) by including their parent "
            "unit's name/code."
        ),
    )
    p.add_argument(
        "--wizard",
        action="store_true",
        help=(
            "Run an interactive prompt that walks through every option instead of passing "
            "flags. Also runs automatically if the tool is started with no arguments at all."
        ),
    )
    p.add_argument(
        "--simplify-tolerance",
        type=float,
        default=None,
        help=(
            "Simplify --aoi-file geometries by this many degrees before sending to Earth "
            "Engine (only used with --breakdown --aoi-file). Needed for detailed admin2/raion "
            "layers with many units, which can exceed Earth Engine's 10MB request-payload limit "
            "otherwise. Try 0.001 (~100m) as a starting point if you hit a "
            "'Request payload size exceeds the limit' error."
        ),
    )
    return p


def _prompt_text(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{label}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default
        print("  (required -- please enter a value)")


def _prompt_choice(label: str, options: list[str]) -> int:
    print(f"\n{label}:")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        val = input(f"Choose 1-{len(options)}: ").strip()
        if val.isdigit() and 1 <= int(val) <= len(options):
            return int(val) - 1
        print(f"  Please enter a number from 1 to {len(options)}.")


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    val = input(f"{label} {suffix}: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def _prompt_single_field(label: str, available_fields: Optional[list[str]]) -> str:
    """Like _prompt_text, but when `available_fields` is a known, non-empty
    list (from list_file_fields/list_gaul_fields), re-prompts until the
    answer is exactly one of them -- catching a typo, or someone pasting a
    comma-separated list where one field name was expected, right where it
    happened instead of deep inside a later Earth Engine call.
    """
    while True:
        val = _prompt_text(label)
        if not available_fields or val in available_fields:
            return val
        print(f"  '{val}' isn't one of the columns/fields listed above -- enter exactly one.")


def _prompt_optional_field(label: str, available_fields: Optional[list[str]]) -> str:
    """Like _prompt_single_field, but blank is an accepted answer (for an
    optional field like a unique-ID column that not every boundary file has).
    """
    while True:
        val = _prompt_text(label, default="").strip()
        if not val or not available_fields or val in available_fields:
            return val
        print(f"  '{val}' isn't one of the columns/fields listed above -- enter exactly one, or leave blank.")


def _prompt_field_list(label: str, available_fields: Optional[list[str]]) -> str:
    """Like _prompt_text with an empty default, but when `available_fields` is
    known, validates every comma-separated entry against it and re-prompts
    (listing which ones didn't match) rather than passing bad names through
    to fail later.

    On a bad entry, the retry prompt is pre-filled with just the valid names
    from the rejected attempt (as its default), so fixing one typo in a long
    comma list doesn't mean retyping the whole thing from scratch -- pressing
    Enter accepts the trimmed list, or the user can type a fresh full list.
    Without this, a single mistyped name in a six-column request would
    otherwise tempt the user into re-entering a short list "for now" and
    silently losing the columns they actually wanted.
    """
    default = ""
    while True:
        val = _prompt_text(label, default=default)
        if not val.strip() or not available_fields:
            return val
        requested = [f.strip() for f in val.split(",") if f.strip()]
        unknown = [f for f in requested if f not in available_fields]
        if not unknown:
            return val
        known = [f for f in requested if f not in unknown]
        default = ", ".join(known)
        print(
            f"  {unknown} not found in the columns/fields listed above -- try again "
            f"(the rest looked fine -- press Enter to keep just {default or 'none'}, "
            "or type the full corrected list)."
        )


def list_file_fields(aoi_file: str) -> list[str]:
    """Return the non-geometry column names in a boundary file, for showing the
    wizard user what's actually available before asking them to name a field.
    Raises whatever geopandas raises on a bad path/format -- callers decide how
    to handle that (the wizard treats it as "couldn't look it up", not fatal).
    """
    import geopandas as gpd

    gdf = gpd.read_file(aoi_file)
    return [c for c in gdf.columns if c != "geometry"]


def list_gaul_fields(aoi_name: str, breakdown: str, ee_project: Optional[str] = None) -> list[str]:
    """Return the property names on one sample FAO GAUL feature for `aoi_name`
    at the given breakdown level, for the same reason as list_file_fields above.
    Initializes Earth Engine itself if needed, since this can run before the
    rest of the wizard would otherwise trigger that.
    """
    import ee

    _ensure_ee_initialized(ee_project)

    if breakdown not in GAUL_BREAKDOWN_LEVELS:
        raise ValueError(
            f"FAO GAUL doesn't have {breakdown} units -- it only goes down to admin2"
        )
    asset_id = GAUL_BREAKDOWN_LEVELS[breakdown][0]
    fc = ee.FeatureCollection(asset_id).filter(ee.Filter.eq("ADM0_NAME", aoi_name))
    if fc.size().getInfo() == 0:
        return []
    props = fc.first().propertyNames().getInfo()
    return [p for p in props if p != "system:index"]


def run_wizard() -> list[str]:
    """Interactively ask for each option and return the equivalent argv list.

    This deliberately doesn't duplicate any validation or execution logic --
    it just builds the same flags a command-line invocation would pass, which
    then go through the normal build_arg_parser().parse_args() + main() path
    below. That keeps there being exactly one code path that actually runs
    the tool, whether it was configured via flags or via this wizard.
    """
    print("VIIRS Nightlight Tool -- interactive setup")
    print("(Ctrl+C at any point to cancel)\n")

    argv: list[str] = []
    ee_project: Optional[str] = None  # asked for once, as soon as it's actually needed
    ee_project_asked = False  # distinguishes "asked, left blank" from "not asked yet"

    aoi_choice = _prompt_choice(
        "Area of interest",
        [
            "Look up a country by ISO 3166-1 alpha-3 code (e.g. 'UKR')",
            "Look up a country or admin unit by name (FAO GAUL)",
            "Supply my own boundary file (shapefile, GeoJSON, or geodatabase)",
        ],
    )
    gaul_country_name: Optional[str] = None  # resolved lazily, only if actually needed below
    if aoi_choice == 0:
        aoi_iso3 = _prompt_text("ISO3 country code (e.g. 'UKR', 'USA', 'KOR')").strip().upper()
        argv += ["--aoi-iso3", aoi_iso3]
    elif aoi_choice == 1:
        aoi_name = _prompt_text("Country or admin-unit name (e.g. 'Ukraine')")
        argv += ["--aoi-name", aoi_name]
        gaul_country_name = aoi_name
    else:
        aoi_file = _prompt_text("Path to your boundary file")
        argv += ["--aoi-file", aoi_file]

    admin_level_labels = [
        "Admin 0 -- whole AOI as a single unit (one time series)",
        "Admin 1 (e.g. oblast/governorate/state)",
        "Admin 2 (e.g. raion/district/municipio)",
        "Admin 3 (e.g. commune/ward)",
        "Admin 4 (e.g. village/sub-ward)",
        "Admin 5 (finest level your data has)",
    ]
    if aoi_choice == 2:  # own file -- the level is just a label, any depth is fine
        granularity_options = admin_level_labels
    else:  # GAUL-backed -- FAO GAUL 2015 only carries country/admin1/admin2
        granularity_options = admin_level_labels[:3]
        print(
            "\n(FAO GAUL only goes down to admin2 -- for admin3 or finer, supply your "
            "own boundary file instead.)"
        )
    breakdown_choice = _prompt_choice("Granularity", granularity_options)
    if breakdown_choice != 0:
        breakdown = f"admin{breakdown_choice}"
        argv += ["--breakdown", breakdown]

        if aoi_choice == 2:  # own file
            available_fields: list[str] = []
            try:
                available_fields = list_file_fields(aoi_file)
            except Exception as e:  # noqa: BLE001
                print(f"  (Couldn't read {aoi_file} to list its columns: {e})")
            if available_fields:
                print(f"\nColumns found in {aoi_file}:")
                for f in available_fields:
                    print(f"  - {f}")

            argv += [
                "--unit-name-field",
                _prompt_single_field("Which column names each unit", available_fields),
            ]

            unit_id_field = _prompt_optional_field(
                "Which column uniquely identifies each unit, e.g. a pcode "
                "(recommended, especially if unit names might repeat -- blank to skip)",
                available_fields,
            )
            if unit_id_field:
                argv += ["--unit-id-field", unit_id_field]

            file_size_mb = None
            try:
                file_size_mb = Path(aoi_file).stat().st_size / (1024 * 1024)
            except OSError:
                pass
            tolerance_prompt = (
                "Simplify geometries by this many degrees before sending to Earth Engine "
                "(blank to skip; try 0.001 for ~100m if you hit a "
                "'Request payload size exceeds the limit' error)"
            )
            if file_size_mb and file_size_mb > 2:
                print(
                    f"\n{aoi_file} is {file_size_mb:.1f} MB -- a detailed boundary file this "
                    "size can exceed Earth Engine's 10MB request-payload limit once every "
                    "unit's full geometry is sent as part of a --breakdown query."
                )
            while True:
                tolerance = _prompt_text(tolerance_prompt, default="").strip()
                if not tolerance:
                    break
                try:
                    float(tolerance)
                    argv += ["--simplify-tolerance", tolerance]
                    break
                except ValueError:
                    print(f"  '{tolerance}' isn't a number -- enter a decimal-degree value or leave blank.")
        else:  # GAUL-backed, either ISO3 or name
            ee_project = _prompt_text(
                "Earth Engine cloud project ID (blank if your account doesn't need one)",
                default="",
            ).strip() or None
            ee_project_asked = True

            if gaul_country_name is None:  # came in via ISO3 -- resolve it to look up fields
                try:
                    gaul_country_name = resolve_iso3_to_gaul_name(aoi_iso3, ee_project)
                    print(f"ISO3 {aoi_iso3!r} matched FAO GAUL country {gaul_country_name!r}.")
                except Exception as e:  # noqa: BLE001
                    print(f"  (Couldn't resolve ISO3 {aoi_iso3!r} to a GAUL country yet: {e})")

            print(
                f"(Each unit's unique ID will be filled in automatically from GAUL's own "
                f"ADM{1 if breakdown == 'admin1' else 2}_CODE -- no need to pick one.)"
            )

            available_fields = []
            if gaul_country_name:
                print(
                    f"\nLooking up available fields on FAO GAUL {breakdown} units for "
                    f"{gaul_country_name!r} ..."
                )
                try:
                    available_fields = list_gaul_fields(gaul_country_name, breakdown, ee_project)
                except Exception as e:  # noqa: BLE001
                    print(f"  (Couldn't look up GAUL fields: {e})")
            if available_fields:
                print(f"Fields available on GAUL {breakdown} units for {gaul_country_name!r}:")
                for f in available_fields:
                    print(f"  - {f}")
            else:
                print(
                    "  (Couldn't confirm available fields -- common GAUL fields are "
                    "ADM0_NAME, ADM0_CODE, ADM1_NAME, ADM1_CODE"
                    + (", ADM2_NAME, ADM2_CODE" if breakdown == "admin2" else "")
                    + ", STATUS, DISP_AREA)"
                )

        attrs = _prompt_field_list(
            "Extra attribute columns to include, comma-separated "
            "(blank for defaults -- see README)",
            available_fields,
        )
        if attrs.strip():
            argv += ["--attributes", attrs.strip()]

    argv += ["--start", _prompt_text("Start date (YYYY-MM-DD, inclusive)")]
    argv += ["--end", _prompt_text("End date (YYYY-MM-DD, exclusive)")]

    freq_options = list(VALID_FREQS)
    freq_choice = _prompt_choice("Frequency", freq_options)
    argv += ["--freq", freq_options[freq_choice]]

    argv += ["--out", _prompt_text("Output CSV path", default="out/nightlights.csv")]

    if _prompt_yes_no(
        "Also write spatial output (GeoJSON/Shapefile), joined by the unit's unique "
        "ID/pcode -- one file per period plus one combined file with every period?",
        default=False,
    ):
        geo_out = _prompt_text(
            "Spatial output path (.geojson or .shp)", default="out/nightlights.geojson"
        ).strip()
        if geo_out:
            argv += ["--geo-out", geo_out]

    if _prompt_yes_no(
        "Add change-vs-previous-period columns (per unit if --breakdown)?", default=False
    ):
        argv.append("--include-change")

    if _prompt_yes_no(
        "Add change-vs-a-fixed-baseline-period columns (e.g. a pre-war baseline)?",
        default=False,
    ):
        baseline_period = _prompt_text(
            f"Baseline period, as a {freq_options[freq_choice]} period label "
            f"(e.g. {_PERIOD_LABEL_EXAMPLES[freq_options[freq_choice]]!r})"
        ).strip()
        if baseline_period:
            argv += ["--baseline-period", baseline_period]

    if breakdown_choice == 0:
        if _prompt_yes_no("Also write a chart PNG next to the CSV?", default=True):
            argv.append("--chart")
    else:
        if _prompt_yes_no(
            "Also write a chart PNG (one mini chart per unit) next to the CSV?",
            default=True,
        ):
            argv.append("--chart")
            chart_units = _prompt_text(
                "Which units to chart, comma-separated exact unit_name values "
                f"(blank to chart all, up to {DEFAULT_MAX_BREAKDOWN_CHART_PANELS})",
                default="",
            )
            if chart_units.strip():
                argv += ["--chart-units", chart_units.strip()]

    if not ee_project_asked:
        ee_project = _prompt_text(
            "Earth Engine cloud project ID (blank if your account doesn't need one)", default=""
        ).strip() or None
    if ee_project:
        argv += ["--ee-project", ee_project]

    print()
    return argv


def build_argv_from_form(fields: dict) -> list[str]:
    """Pure function: turn a plain dict of form values into the argv list
    build_arg_parser().parse_args() expects.

    This is the GUI's equivalent of run_wizard() -- same one-code-path
    principle (build an argv list, then let build_arg_parser()/main() do the
    actual validation and work), but driven by a plain dict of already-chosen
    values instead of interactive input() prompts, so it's usable from a
    Tkinter form (or tested directly, with no widgets involved at all).

    Expected keys (all optional except as noted; unmentioned/None/empty
    values are treated as "not set"):
        aoi_source: "iso3" | "name" | "file"  (required)
        aoi_iso3, aoi_name, aoi_file: str -- whichever matches aoi_source
        breakdown_level: int 0-5 (0 or omitted = no --breakdown, whole AOI)
        unit_name_field: str -- required when aoi_source == "file" and
            breakdown_level > 0
        unit_id_field: str
        simplify_tolerance: str/float -- only meaningful with aoi_source == "file"
        attributes: str (comma-separated) or list[str]
        start, end: str, YYYY-MM-DD (required)
        freq: one of VALID_FREQS (required)
        out: str, output CSV path (required)
        geo_out: str -- optional path for a joined spatial output (.geojson or .shp),
            written as one file per period plus one combined file with every period
        include_change: bool -- add <stat>_change_abs/_pct columns vs the previous period
        baseline_period: str -- period label (matching freq's format) to add
            <stat>_vs_baseline_abs/_pct columns against
        chart: bool
        chart_units: str (comma-separated) or list[str] -- only used if chart
            and breakdown_level are both set
        ee_project: str

    Raises ValueError with a message fit to show the user directly (e.g. in
    a message box) when something required is missing or inconsistent --
    the same checks build_arg_parser()/main() would eventually surface, just
    caught earlier with a friendlier message than an argparse error or a
    raw exception partway through a run.
    """
    argv: list[str] = []

    aoi_source = fields.get("aoi_source")
    if aoi_source == "iso3":
        iso3 = (fields.get("aoi_iso3") or "").strip().upper()
        if not iso3:
            raise ValueError("Enter an ISO3 country code (e.g. 'UKR').")
        argv += ["--aoi-iso3", iso3]
    elif aoi_source == "name":
        name = (fields.get("aoi_name") or "").strip()
        if not name:
            raise ValueError("Enter a country or admin-unit name.")
        argv += ["--aoi-name", name]
    elif aoi_source == "file":
        path = (fields.get("aoi_file") or "").strip()
        if not path:
            raise ValueError("Choose a boundary file.")
        argv += ["--aoi-file", path]
    else:
        raise ValueError("Choose an area-of-interest source (ISO3 code, name, or file).")

    breakdown_level = fields.get("breakdown_level") or 0
    if breakdown_level:
        if breakdown_level not in (1, 2, 3, 4, 5):
            raise ValueError("Granularity must be Admin 0-5.")
        if aoi_source != "file" and breakdown_level > 2:
            raise ValueError(
                f"Admin{breakdown_level} isn't available with a country lookup -- FAO GAUL "
                "only carries admin1/admin2 below country level. Supply your own boundary "
                "file for admin3 or finer."
            )
        argv += ["--breakdown", f"admin{breakdown_level}"]

        if aoi_source == "file":
            unit_name_field = (fields.get("unit_name_field") or "").strip()
            if not unit_name_field:
                raise ValueError("Choose which column names each unit.")
            argv += ["--unit-name-field", unit_name_field]

            unit_id_field = (fields.get("unit_id_field") or "").strip()
            if unit_id_field:
                argv += ["--unit-id-field", unit_id_field]

            tolerance = fields.get("simplify_tolerance")
            if tolerance not in (None, ""):
                try:
                    float(tolerance)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"Simplify tolerance {tolerance!r} isn't a number -- enter a "
                        "decimal-degree value (e.g. 0.001) or leave it blank."
                    )
                argv += ["--simplify-tolerance", str(tolerance)]

        attributes = fields.get("attributes")
        if attributes:
            if isinstance(attributes, (list, tuple)):
                attributes = ",".join(a for a in attributes if a)
            attributes = attributes.strip()
            if attributes:
                argv += ["--attributes", attributes]

    start = (fields.get("start") or "").strip()
    end = (fields.get("end") or "").strip()
    if not start or not end:
        raise ValueError("Enter both a start and end date (YYYY-MM-DD).")
    argv += ["--start", start, "--end", end]

    freq = fields.get("freq")
    if freq not in VALID_FREQS:
        raise ValueError(f"Choose a frequency ({', '.join(VALID_FREQS)}).")
    argv += ["--freq", freq]

    out = (fields.get("out") or "").strip()
    if not out:
        raise ValueError("Choose an output CSV path.")
    argv += ["--out", out]

    geo_out = (fields.get("geo_out") or "").strip()
    if geo_out:
        argv += ["--geo-out", geo_out]

    if fields.get("include_change"):
        argv.append("--include-change")

    baseline_period = (fields.get("baseline_period") or "").strip()
    if baseline_period:
        argv += ["--baseline-period", baseline_period]

    if fields.get("chart"):
        argv.append("--chart")
        if breakdown_level:
            chart_units = fields.get("chart_units")
            if isinstance(chart_units, (list, tuple)):
                chart_units = ",".join(c for c in chart_units if c)
            if chart_units:
                chart_units = chart_units.strip()
                if chart_units:
                    argv += ["--chart-units", chart_units]

    ee_project = (fields.get("ee_project") or "").strip()
    if ee_project:
        argv += ["--ee-project", ee_project]

    return argv


def main(argv: Optional[list[str]] = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if not raw_argv or "--wizard" in raw_argv:
        try:
            argv = run_wizard()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.", file=sys.stderr)
            return 1

    args = build_arg_parser().parse_args(argv)
    attribute_fields = (
        [f.strip() for f in args.attributes.split(",") if f.strip()] if args.attributes else None
    )

    periods = build_periods(args.start, args.end, args.freq)
    if not periods:
        print("No periods to process — check --start/--end/--freq.", file=sys.stderr)
        return 1
    if args.freq in ("daily", "weekly") and len(periods) > 366:
        print(
            f"Warning: {len(periods)} {args.freq} periods requested — this will make "
            f"{len(periods)} separate Earth Engine calls and may be slow.",
            file=sys.stderr,
        )

    import ee

    try:
        if args.ee_project:
            ee.Initialize(project=args.ee_project)
        else:
            ee.Initialize()
    except Exception as e:  # noqa: BLE001
        print(
            "Earth Engine initialization failed. Have you run "
            "`earthengine authenticate`? See README.md.\n"
            f"Original error: {e}",
            file=sys.stderr,
        )
        return 1

    if args.aoi_iso3:
        try:
            resolved_name = resolve_iso3_to_gaul_name(args.aoi_iso3)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(
            f"ISO3 code {args.aoi_iso3.upper()!r} matched FAO GAUL country {resolved_name!r}.",
            file=sys.stderr,
        )
        # From here on, treat it exactly like --aoi-name — every other code path
        # (single-AOI lookup, --breakdown, etc.) already knows how to handle that.
        args.aoi_name = resolved_name

    rows = []
    unit_name_column = "unit_name"
    unit_id_column: Optional[str] = "unit_id"

    if args.breakdown:
        fc = resolve_breakdown_collection(
            args.aoi_file,
            args.aoi_name,
            args.breakdown,
            args.unit_name_field,
            simplify_tolerance=args.simplify_tolerance,
            attribute_fields=attribute_fields,
            unit_id_field=args.unit_id_field,
        )
        # Output columns should say what they actually are -- the column the
        # user picked as --unit-name-field/--unit-id-field for a boundary
        # file, or the GAUL property that filled them in automatically for a
        # country/name lookup -- rather than a generic 'unit_name'/'unit_id'
        # label. See rename_unit_columns() for where this is applied.
        if args.aoi_file:
            unit_name_column = args.unit_name_field
            unit_id_column = args.unit_id_field or "unit_id"
        else:
            unit_name_column, unit_id_column = gaul_unit_name_id_fields(args.breakdown)
        unit_count = fc.size().getInfo()
        print(f"Breaking down into {unit_count} {args.breakdown} units.", file=sys.stderr)
        for i, period in enumerate(periods, 1):
            print(f"[{i}/{len(periods)}] {period.label} ...", file=sys.stderr)
            rows.extend(
                fetch_period_breakdown_stats(
                    args.freq, fc, period, attribute_fields=attribute_fields
                )
            )
    else:
        aoi_geom = resolve_aoi_geometry(args.aoi_file, args.aoi_name)
        for i, period in enumerate(periods, 1):
            print(f"[{i}/{len(periods)}] {period.label} ...", file=sys.stderr)
            rows.append(fetch_period_stats(args.freq, aoi_geom, period))

    change_group_key = _chart_key if args.breakdown else None

    if args.include_change:
        rows = compute_period_over_period_change(rows, group_key=change_group_key)

    if args.baseline_period:
        try:
            baseline_period = parse_period_label(args.baseline_period, args.freq)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"Fetching baseline period {baseline_period.label} ...", file=sys.stderr)
        if args.breakdown:
            baseline_rows = fetch_period_breakdown_stats(
                args.freq, fc, baseline_period, attribute_fields=attribute_fields
            )
        else:
            baseline_rows = [fetch_period_stats(args.freq, aoi_geom, baseline_period)]
        rows = compute_baseline_change(rows, baseline_rows, group_key=change_group_key)

    out_path = Path(args.out)
    csv_rows = (
        rename_unit_columns(rows, unit_name_column, unit_id_column) if args.breakdown else rows
    )
    write_csv(csv_rows, out_path)
    print(f"Wrote {len(rows)} rows to {out_path}")

    if args.geo_out:
        geo_out_path = Path(args.geo_out)
        missing_keys: set = set()

        if args.breakdown:
            unit_geoms = fetch_unit_geometries(fc)
            # _chart_key() (unit_id when present, else unit_name) doubles as
            # the join key here -- same "which unit is this really" logic
            # --chart-units matching already relies on, applied to the raw
            # (pre-rename) properties fetch_unit_geometries() returns.
            geom_by_key = {_chart_key(g): g["geometry"] for g in unit_geoms}

            def geometry_for_row(row):
                key = _chart_key(
                    {
                        "unit_id": row.get(unit_id_column) if unit_id_column else None,
                        "unit_name": row.get(unit_name_column),
                    }
                )
                geom = geom_by_key.get(key)
                if geom is None:
                    missing_keys.add(key)
                return geom
        else:
            whole_aoi_geometry = fetch_whole_aoi_geometry(aoi_geom)

            def geometry_for_row(row):
                return whole_aoi_geometry

        geo_rows = attach_geometry(csv_rows, geometry_for_row)
        if missing_keys:
            print(
                f"Warning: {len(missing_keys)} unit(s) had no matching geometry and were "
                f"left out of the spatial output: {sorted(missing_keys, key=str)}",
                file=sys.stderr,
            )
            geo_rows = [r for r in geo_rows if r["geometry"] is not None]

        rows_by_period = split_rows_by_period(geo_rows)
        try:
            per_period_paths = write_geo_outputs_per_period(rows_by_period, geo_out_path)
            combined_path = write_geo_outputs_combined(geo_rows, geo_out_path)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(
            f"Wrote {len(per_period_paths)} spatial file(s), one per period: "
            f"{', '.join(str(p) for p in per_period_paths.values())}"
        )
        print(f"Wrote 1 combined spatial file with every period (one unit-period per row): {combined_path}")

    if args.chart:
        chart_path = out_path.with_suffix(".png")
        if args.breakdown:
            chart_units = (
                [u.strip() for u in args.chart_units.split(",") if u.strip()]
                if args.chart_units
                else None
            )
            all_units = select_breakdown_chart_units(rows, unit_filter=chart_units, max_panels=len(rows) + 1)
            units = all_units if chart_units else all_units[:DEFAULT_MAX_BREAKDOWN_CHART_PANELS]
            if not units:
                print(
                    "Note: --chart-units matched no units in the output -- skipping chart. "
                    "Check the spelling against the unit_name column.",
                    file=sys.stderr,
                )
            else:
                if chart_units is None and len(all_units) > len(units):
                    print(
                        f"Note: charting the first {len(units)} of {len(all_units)} units "
                        "-- use --chart-units to pick specific ones instead.",
                        file=sys.stderr,
                    )
                write_breakdown_chart(rows, chart_path, units)
                print(f"Wrote chart ({len(units)} unit panels) to {chart_path}")
        else:
            write_chart(rows, chart_path)
            print(f"Wrote chart to {chart_path}")

    flagged = [r for r in rows if r.get("qa_flag") not in (None, "ok")]
    if flagged:
        print(
            f"Note: {len(flagged)}/{len(rows)} periods flagged (see qa_flag column) "
            "— treat those values with caution.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
