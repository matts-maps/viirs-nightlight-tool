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


def test_form_is_wrapped_in_a_scrollable_canvas():
    # The whole form (not just the attribute checkbox panel) is embedded in
    # a canvas with a scrollbar, so it stays usable on a short screen. Not
    # a pixel-level scroll check (no real display here), just confirming
    # the wrapper and its wheel handler are actually wired up.
    root, gui = _make_gui()
    try:
        assert isinstance(gui._outer_canvas, tk.Canvas)
        assert gui._outer_canvas.grid_info()
        # Should not raise even off-screen/unmapped.
        gui._on_root_mousewheel(type("Event", (), {"delta": 120, "num": None})())
    finally:
        _destroy(root)


def test_geoextent_field_always_visible():
    # The geoextent code feeds every output filename -- it's optional (and
    # overrides the automatic ISO3 code) with --aoi-iso3, required
    # otherwise, but shown unconditionally either way so it's never a
    # surprise which filename a run is going to produce.
    root, gui = _make_gui()
    try:
        assert gui.geoextent_entry.grid_info()

        gui.aoi_source.set("name")
        gui._on_aoi_source_change()
        assert gui.geoextent_entry.grid_info()

        gui.aoi_source.set("file")
        gui._on_aoi_source_change()
        assert gui.geoextent_entry.grid_info()

        gui.aoi_source.set("iso3")
        gui._on_aoi_source_change()
        assert gui.geoextent_entry.grid_info()
    finally:
        _destroy(root)


def test_vector_geojson_and_shapefile_are_mutually_exclusive_checkboxes():
    # Both are always visible (no gating checkbox), and unlike a Radiobutton
    # pair, both can be off at once -- ticking one clears the other rather
    # than forcing a choice.
    root, gui = _make_gui()
    try:
        assert gui.vector_format_geojson_cb.grid_info()
        assert gui.vector_format_shapefile_cb.grid_info()

        gui.vector_geojson.set(True)
        gui._on_vector_format_change("geojson")
        assert gui.vector_shapefile.get() is False

        gui.vector_shapefile.set(True)
        gui._on_vector_format_change("shapefile")
        assert gui.vector_geojson.get() is False

        gui.vector_shapefile.set(False)
        assert gui.vector_geojson.get() is False
        assert gui.vector_shapefile.get() is False
    finally:
        _destroy(root)


def test_raster_scale_field_always_visible_and_defaults_to_300():
    root, gui = _make_gui()
    try:
        assert gui.raster_scale_entry.grid_info()
        assert gui.raster_scale.get() == "300"

        gui.raster_whole_aoi.set(True)
        assert gui.raster_scale_entry.grid_info()

        gui.raster_whole_aoi.set(False)
        assert gui.raster_scale_entry.grid_info()
    finally:
        _destroy(root)


def test_vector_format_defaults_to_shapefile():
    root, gui = _make_gui()
    try:
        assert gui.vector_shapefile.get() is True
        assert gui.vector_geojson.get() is False
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
        gui.vector_shapefile.set(True)
        gui.vector_geojson.set(False)
        gui.raster_whole_aoi.set(True)
        gui.raster_yoy.set(False)

        fields = gui._collect_fields()
        assert fields["out_dir"] == "out"
        assert fields["vector_out"] == "shapefile"
        assert fields["raster_whole_aoi"] is True
        assert fields["raster_yoy"] is False

        gui.vector_shapefile.set(False)
        fields = gui._collect_fields()
        assert fields["vector_out"] is None
    finally:
        _destroy(root)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
