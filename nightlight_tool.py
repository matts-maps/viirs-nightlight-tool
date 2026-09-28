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
    """
    row: dict = {
        "period": period_label,
        "unit_name": feature_properties.get("unit_name"),
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


def select_breakdown_chart_units(
    rows: list[dict],
    unit_filter: Optional[list[str]] = None,
    max_panels: int = DEFAULT_MAX_BREAKDOWN_CHART_PANELS,
) -> list[str]:
    """Pure function: decide which units a --breakdown chart should draw one
    panel for, and in what order.

    With `unit_filter` given (exact unit_name values, as the user typed them),
    returns just those, in the order given -- deliberate selection needs no
    cap. Without a filter, returns every unit that appears in `rows`, in
    first-seen order, unless that's more than `max_panels`, in which case it
    truncates and the caller is expected to warn (a chart with hundreds of
    tiny panels isn't useful, and the fix is to either narrow with
    --chart-units or just pivot the CSV yourself for a full per-unit view).
    """
    seen: list[str] = []
    seen_set = set()
    for row in rows:
        name = row.get("unit_name")
        if name is not None and name not in seen_set:
            seen.append(name)
            seen_set.add(name)

    if unit_filter:
        return [name for name in unit_filter if name in seen_set]

    return seen[:max_panels]


def write_breakdown_chart(
    rows: list[dict],
    chart_path: Path,
    units: list[str],
    value_field: str = "mean_radiance",
) -> None:
    """Write a small-multiples PNG: one mini line chart per unit in `units`,
    each showing `value_field` over `period`. Companion to write_chart() for
    --breakdown output, where a single shared chart with one line per unit
    isn't legible once there are more than a handful of units.
    """
    import math

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not units:
        raise ValueError("no units to chart")

    by_unit: dict[str, list[dict]] = {u: [] for u in units}
    for row in rows:
        name = row.get("unit_name")
        if name in by_unit:
            by_unit[name].append(row)

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
        ax.set_title(str(unit), fontsize=9)
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


def resolve_breakdown_collection(
    aoi_file: Optional[str],
    aoi_name: Optional[str],
    breakdown: str,
    unit_name_field: Optional[str],
    simplify_tolerance: Optional[float] = None,
    attribute_fields: Optional[list[str]] = None,
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
    """
    import ee

    if aoi_name:
        level_map = {
            "admin1": ("FAO/GAUL/2015/level1", "ADM1_NAME"),
            "admin2": ("FAO/GAUL/2015/level2", "ADM2_NAME"),
        }
        asset_id, name_field = level_map[breakdown]
        fc = ee.FeatureCollection(asset_id).filter(ee.Filter.eq("ADM0_NAME", aoi_name))
        count = fc.size().getInfo()
        if count == 0:
            raise ValueError(
                f"No {breakdown} units found for country name {aoi_name!r} in "
                f"{asset_id}. GAUL's country naming can differ from common usage "
                "— check spelling/capitalisation."
            )
        return fc.map(lambda f: f.set("unit_name", f.get(name_field)))

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
        if attribute_fields:
            missing = [f for f in attribute_fields if f not in gdf.columns]
            if missing:
                raise ValueError(
                    f"--attributes field(s) {missing} not found in {aoi_file} columns: "
                    f"{list(gdf.columns)}"
                )
        features = []
        for _, row in gdf.iterrows():
            properties = {"unit_name": row[unit_name_field]}
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
        return [
            build_breakdown_row(
                period.label,
                band,
                {"unit_name": name},
                scene_count=0,
                attribute_fields=attribute_fields,
            )
            for name in unit_names
        ]

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
        choices=("admin1", "admin2"),
        default=None,
        help=(
            "Instead of one AOI-wide row per period, output one row per admin unit "
            "per period. With --aoi-name, looks up FAO GAUL admin1/admin2 units "
            "within that country. With --aoi-file, keeps each feature in the file "
            "separate (needs --unit-name-field)."
        ),
    )
    p.add_argument(
        "--unit-name-field",
        default=None,
        help="Property/column in --aoi-file to label each unit with, when using --breakdown with --aoi-file",
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


def _prompt_field_list(label: str, available_fields: Optional[list[str]]) -> str:
    """Like _prompt_text with an empty default, but when `available_fields` is
    known, validates every comma-separated entry against it and re-prompts
    (listing which ones didn't match) rather than passing bad names through
    to fail later.
    """
    while True:
        val = _prompt_text(label, default="")
        if not val.strip() or not available_fields:
            return val
        requested = [f.strip() for f in val.split(",") if f.strip()]
        unknown = [f for f in requested if f not in available_fields]
        if not unknown:
            return val
        print(f"  {unknown} not found in the columns/fields listed above -- try again.")


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

    asset_id = {"admin1": "FAO/GAUL/2015/level1", "admin2": "FAO/GAUL/2015/level2"}[breakdown]
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

    breakdown_choice = _prompt_choice(
        "Granularity",
        [
            "Whole AOI as a single unit (one time series)",
            "Break down by admin1 (e.g. oblast/governorate/state)",
            "Break down by admin2 (e.g. raion/district/municipio)",
        ],
    )
    if breakdown_choice != 0:
        breakdown = "admin1" if breakdown_choice == 1 else "admin2"
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

    if args.breakdown:
        fc = resolve_breakdown_collection(
            args.aoi_file,
            args.aoi_name,
            args.breakdown,
            args.unit_name_field,
            simplify_tolerance=args.simplify_tolerance,
            attribute_fields=attribute_fields,
        )
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

    out_path = Path(args.out)
    write_csv(rows, out_path)
    print(f"Wrote {len(rows)} rows to {out_path}")

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
