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

VALID_FREQS = ("daily", "monthly", "annual")

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
    period_label: str, band: str, feature_properties: dict, scene_count: int
) -> dict:
    """Pure function: turn one reduceRegions-output feature's properties into a row.

    `feature_properties` is a plain dict (as returned by ee's getInfo(), or a fake
    one in tests) — this function makes no Earth Engine calls itself, which is what
    keeps it unit-testable without an EE session.
    """
    return {
        "period": period_label,
        "unit_name": feature_properties.get("unit_name"),
        "admin0_name": feature_properties.get("ADM0_NAME"),
        "admin1_name": feature_properties.get("ADM1_NAME"),
        "admin2_name": feature_properties.get("ADM2_NAME"),
        "mean_radiance": _get_stat(feature_properties, band, "mean"),
        "sum_radiance": _get_stat(feature_properties, band, "sum"),
        "median_radiance": _get_stat(feature_properties, band, "median"),
        "valid_pixel_count": _get_stat(feature_properties, band, "count"),
        "scene_count": scene_count,
        "qa_flag": qa_flag(scene_count, None),
    }


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


# ---------------------------------------------------------------------------
# Earth Engine glue — only this half touches `ee`.
# ---------------------------------------------------------------------------

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

    # daily
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
):
    """Return an ee.FeatureCollection of sub-units to break the analysis down by,
    each carrying a 'unit_name' property.

    --aoi-name + --breakdown: looks up FAO GAUL admin1/admin2 units within that
    country. --aoi-file + --breakdown: keeps every feature in the file separate
    (rather than dissolving them, like the single-AOI path does) and labels each
    from --unit-name-field.
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
        features = [
            ee.Feature(
                ee.Geometry(simplify_geometry(row.geometry, simplify_tolerance).__geo_interface__),
                {"unit_name": row[unit_name_field]},
            )
            for _, row in gdf.iterrows()
        ]
        return ee.FeatureCollection(features)

    raise ValueError("--breakdown needs either --aoi-name or --aoi-file (+ --unit-name-field)")


def fetch_period_breakdown_stats(freq: str, fc, period: Period, scale: int = 500) -> list[dict]:
    """Query Earth Engine for one period's zonal stats across every unit in `fc`
    in a single reduceRegions call, rather than one call per unit."""
    image, band, scene_count = _get_period_image_and_scene_count(freq, period)

    if image is None:
        unit_names = fc.aggregate_array("unit_name").getInfo()
        return [
            build_breakdown_row(period.label, band, {"unit_name": name}, scene_count=0)
            for name in unit_names
        ]

    reduced = image.select(band).reduceRegions(
        collection=fc, reducer=_combined_reducer(), scale=scale, tileScale=4
    )
    features = reduced.getInfo()["features"]
    return [
        build_breakdown_row(period.label, band, f["properties"], scene_count)
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
    p.add_argument("--start", required=True, help="Start date, YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="End date, YYYY-MM-DD (exclusive)")
    p.add_argument("--freq", required=True, choices=VALID_FREQS, help="Time step")
    p.add_argument("--out", required=True, help="Output CSV path")
    p.add_argument(
        "--chart", action="store_true", help="Also write a PNG chart next to the CSV"
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


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    periods = build_periods(args.start, args.end, args.freq)
    if not periods:
        print("No periods to process — check --start/--end/--freq.", file=sys.stderr)
        return 1
    if args.freq == "daily" and len(periods) > 366:
        print(
            f"Warning: {len(periods)} daily periods requested — this will make "
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

    rows = []

    if args.breakdown:
        fc = resolve_breakdown_collection(
            args.aoi_file,
            args.aoi_name,
            args.breakdown,
            args.unit_name_field,
            simplify_tolerance=args.simplify_tolerance,
        )
        unit_count = fc.size().getInfo()
        print(f"Breaking down into {unit_count} {args.breakdown} units.", file=sys.stderr)
        for i, period in enumerate(periods, 1):
            print(f"[{i}/{len(periods)}] {period.label} ...", file=sys.stderr)
            rows.extend(fetch_period_breakdown_stats(args.freq, fc, period))
    else:
        aoi_geom = resolve_aoi_geometry(args.aoi_file, args.aoi_name)
        for i, period in enumerate(periods, 1):
            print(f"[{i}/{len(periods)}] {period.label} ...", file=sys.stderr)
            rows.append(fetch_period_stats(args.freq, aoi_geom, period))

    out_path = Path(args.out)
    write_csv(rows, out_path)
    print(f"Wrote {len(rows)} rows to {out_path}")

    if args.chart:
        if args.breakdown:
            print(
                "Note: --chart is skipped in --breakdown mode (one line per unit "
                "isn't a useful chart at admin1/admin2 scale) — pivot the CSV by "
                "unit_name yourself for a per-unit view.",
                file=sys.stderr,
            )
        else:
            chart_path = out_path.with_suffix(".png")
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
