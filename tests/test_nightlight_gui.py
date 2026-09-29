"""
Headless smoke tests for nightlight_gui.py's Outputs section.

These construct the real NightlightGUI against a real (but off-screen) Tk
root, so they exercise actual widget show/hide logic -- not a mock. Needs a
display: run under xvfb-run if there's no X server available, e.g.:

    xvfb-run -a python -m pytest tests/test_nightlight_gui.py

nightlight_gui.py only imports geopandas/shapely/ee lazily inside methods
that actually need them (file/EE lookups, run_main), so constructing the
GUI and exercising the plain widget-state logic here needs no optional
dependency beyond tkinter itself.
"""

import sys
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nightlight_gui import NightlightGUI


def _make_gui():
    try:
        root = tk.Tk()
    except tk.TclError as e:  # pragma: no cover -- environment has no display
        pytest.skip(f"no display available for Tk: {e}")
    root.withdraw()
    gui = NightlightGUI(root)
    return root, gui


def _destroy(root):
    root.update_idletasks()
    root.destroy()


def test_geoextent_field_hidden_for_iso3_by_default():
    # ISO3 is the default AOI source -- the geoextent field is automatic
    # from the ISO3 code, so it should start hidden.
    root, gui = _make_gui()
    try:
        assert not gui.geoextent_entry.grid_info()
    finally:
        _destroy(root)


def test_geoextent_field_shown_for_name_and_file_sources():
    root, gui = _make_gui()
    try:
        gui.aoi_source.set("name")
        gui._on_aoi_source_change()
        assert gui.geoextent_entry.grid_info()

        gui.aoi_source.set("file")
        gui._on_aoi_source_change()
        assert gui.geoextent_entry.grid_info()

        gui.aoi_source.set("iso3")
        gui._on_aoi_source_change()
        assert not gui.geoextent_entry.grid_info()
    finally:
        _destroy(root)


def test_vector_format_radios_hidden_until_vector_checkbox_checked():
    root, gui = _make_gui()
    try:
        assert not gui.vector_format_geojson_rb.grid_info()
        assert not gui.vector_format_shapefile_rb.grid_info()

        gui.vector_out.set(True)
        gui._on_vector_out_change()
        assert gui.vector_format_geojson_rb.grid_info()
        assert gui.vector_format_shapefile_rb.grid_info()

        gui.vector_out.set(False)
        gui._on_vector_out_change()
        assert not gui.vector_format_geojson_rb.grid_info()
    finally:
        _destroy(root)


def test_raster_scale_field_shown_when_either_raster_checkbox_checked():
    root, gui = _make_gui()
    try:
        assert not gui.raster_scale_entry.grid_info()

        gui.raster_whole_aoi.set(True)
        gui._on_raster_change()
        assert gui.raster_scale_entry.grid_info()

        gui.raster_whole_aoi.set(False)
        gui._on_raster_change()
        assert not gui.raster_scale_entry.grid_info()

        gui.raster_yoy.set(True)
        gui._on_raster_change()
        assert gui.raster_scale_entry.grid_info()
    finally:
        _destroy(root)


def test_raster_whole_aoi_and_yoy_are_independent_checkboxes():
    # Checking one shouldn't flip the other -- they're separate BooleanVars.
    root, gui = _make_gui()
    try:
        gui.raster_whole_aoi.set(True)
        assert gui.raster_yoy.get() is False

        gui.raster_whole_aoi.set(False)
        gui.raster_yoy.set(True)
        assert gui.raster_whole_aoi.get() is False
    finally:
        _destroy(root)


def test_csv_checkbox_is_locked_on():
    root, gui = _make_gui()
    try:
        assert gui.csv_always_on.get() is True
    finally:
        _destroy(root)


def test_collect_fields_reflects_outputs_section():
    root, gui = _make_gui()
    try:
        gui.aoi_source.set("iso3")
        gui.aoi_iso3.set("UKR")
        gui.start.set("2026-01-01")
        gui.end.set("2026-02-01")
        gui.out_dir.set("out")
        gui.vector_out.set(True)
        gui.vector_format.set("shapefile")
        gui.raster_whole_aoi.set(True)
        gui.raster_yoy.set(False)

        fields = gui._collect_fields()
        assert fields["out_dir"] == "out"
        assert fields["vector_out"] == "shapefile"
        assert fields["raster_whole_aoi"] is True
        assert fields["raster_yoy"] is False

        gui.vector_out.set(False)
        fields = gui._collect_fields()
        assert fields["vector_out"] is None
    finally:
        _destroy(root)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
