"""
Tkinter GUI front-end for nightlight_tool.py.

Same one-code-path principle as --wizard: this collects form values into a
plain dict, turns it into an argv list with build_argv_from_form(), and
calls main(argv) -- the exact same function both flag-based CLI use and
--wizard use. No parallel logic to keep in sync with the core tool; this
file is pure UI glue.

Run with (same environment as the CLI -- see README for setup/auth):
    python nightlight_gui.py

Requires tkinter, which ships with most Python installs. On some Linux
distros it's a separate OS package, e.g.:
    sudo apt-get install python3-tk
"""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from nightlight_tool import (
    VALID_FREQS,
    build_argv_from_form,
    list_file_fields,
    list_gaul_fields,
    resolve_iso3_to_gaul_name,
)
from nightlight_tool import main as run_main

ADMIN_LEVEL_LABELS = [
    "Whole AOI -- one time series (Admin 0)",
    "Admin 1 (e.g. oblast/governorate/state)",
    "Admin 2 (e.g. raion/district/municipio)",
    "Admin 3 (e.g. commune/ward)",
    "Admin 4 (e.g. village/sub-ward)",
    "Admin 5 (finest level your data has)",
]
GAUL_ADMIN_LEVEL_LABELS = ADMIN_LEVEL_LABELS[:3]  # FAO GAUL only goes to admin2


class _QueueWriter:
    """A minimal, thread-safe, file-like object that pushes writes onto a
    queue instead of a real stream. Standing in for sys.stdout/sys.stderr
    while the worker thread runs lets nightlight_tool.py's ordinary print()
    calls reach the GUI's log panel without nightlight_tool.py needing to
    know anything about Tkinter.
    """

    def __init__(self, q: "queue.Queue[str]") -> None:
        self._queue = q

    def write(self, text: str) -> int:
        if text:
            self._queue.put(text)
        return len(text)

    def flush(self) -> None:  # pragma: no cover -- no-op, required by the file API
        pass


class NightlightGUI:
    """The GUI app.

    Kept as a plain class taking a Tk root, rather than a bare script, so it
    can be driven directly (construct it, set widget values, call
    _on_run()/_collect_fields(), etc.) for headless verification -- no
    simulated clicking needed.
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("VIIRS Nightlight Tool")
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._poll_job: Optional[str] = None

        self._build_widgets()
        self._on_aoi_source_change()
        self._poll_log_queue()

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        pad = {"padx": 6, "pady": 3}
        frm = ttk.Frame(self.root, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        row = 0
        ttk.Label(frm, text="Area of interest", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )
        row += 1

        self.aoi_source = tk.StringVar(value="iso3")

        ttk.Radiobutton(
            frm,
            text="ISO3 country code",
            variable=self.aoi_source,
            value="iso3",
            command=self._on_aoi_source_change,
        ).grid(row=row, column=0, sticky="w", **pad)
        self.aoi_iso3 = tk.StringVar()
        ttk.Entry(frm, textvariable=self.aoi_iso3, width=10).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Radiobutton(
            frm,
            text="Country/admin name (FAO GAUL)",
            variable=self.aoi_source,
            value="name",
            command=self._on_aoi_source_change,
        ).grid(row=row, column=0, sticky="w", **pad)
        self.aoi_name = tk.StringVar()
        ttk.Entry(frm, textvariable=self.aoi_name, width=30).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Radiobutton(
            frm,
            text="My own boundary file",
            variable=self.aoi_source,
            value="file",
            command=self._on_aoi_source_change,
        ).grid(row=row, column=0, sticky="w", **pad)
        self.aoi_file = tk.StringVar()
        ttk.Entry(frm, textvariable=self.aoi_file, width=40).grid(row=row, column=1, sticky="w", **pad)
        ttk.Button(frm, text="Browse...", command=self._on_browse_aoi_file).grid(row=row, column=2, **pad)
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
        row += 1

        ttk.Label(frm, text="Granularity").grid(row=row, column=0, sticky="w", **pad)
        self.granularity = tk.StringVar(value=ADMIN_LEVEL_LABELS[0])
        self.granularity_combo = ttk.Combobox(
            frm, textvariable=self.granularity, values=GAUL_ADMIN_LEVEL_LABELS, state="readonly", width=38
        )
        self.granularity_combo.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        self.granularity_combo.bind("<<ComboboxSelected>>", lambda e: self._on_granularity_change())
        row += 1

        # Fields that only matter once a --breakdown granularity is chosen.
        self.breakdown_frame = ttk.LabelFrame(frm, text="Breakdown options")
        self.breakdown_frame.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        row += 1

        b_row = 0
        ttk.Label(self.breakdown_frame, text="Unit name column").grid(row=b_row, column=0, sticky="w", **pad)
        self.unit_name_field = tk.StringVar()
        self.unit_name_combo = ttk.Combobox(
            self.breakdown_frame, textvariable=self.unit_name_field, width=28, state="disabled"
        )
        self.unit_name_combo.grid(row=b_row, column=1, sticky="w", **pad)
        b_row += 1

        ttk.Label(self.breakdown_frame, text="Unique ID column (pcode, recommended)").grid(
            row=b_row, column=0, sticky="w", **pad
        )
        self.unit_id_field = tk.StringVar()
        self.unit_id_combo = ttk.Combobox(
            self.breakdown_frame, textvariable=self.unit_id_field, width=28, state="disabled"
        )
        self.unit_id_combo.grid(row=b_row, column=1, sticky="w", **pad)
        b_row += 1

        ttk.Label(self.breakdown_frame, text="Extra attribute columns\n(tick any you want)").grid(
            row=b_row, column=0, sticky="nw", **pad
        )
        # A checkbox per available column, not a multi-select listbox -- ticking
        # a box is more discoverable than knowing to ctrl/shift-click. Built as
        # a scrollable canvas of ttk.Checkbuttons since the column count varies
        # (a detailed boundary file can carry dozens of properties) and a plain
        # frame wouldn't scroll.
        attrs_outer = ttk.Frame(self.breakdown_frame)
        attrs_outer.grid(row=b_row, column=1, sticky="w", **pad)
        self.attributes_canvas = tk.Canvas(attrs_outer, height=110, width=260, highlightthickness=0)
        attrs_scrollbar = ttk.Scrollbar(attrs_outer, orient="vertical", command=self.attributes_canvas.yview)
        self.attributes_inner = ttk.Frame(self.attributes_canvas)
        self.attributes_inner.bind(
            "<Configure>",
            lambda e: self.attributes_canvas.configure(scrollregion=self.attributes_canvas.bbox("all")),
        )
        self.attributes_canvas.create_window((0, 0), window=self.attributes_inner, anchor="nw")
        self.attributes_canvas.configure(yscrollcommand=attrs_scrollbar.set)
        self.attributes_canvas.pack(side="left", fill="both", expand=True)
        attrs_scrollbar.pack(side="right", fill="y")
        self._attribute_vars: dict[str, tk.BooleanVar] = {}
        b_row += 1

        ttk.Label(self.breakdown_frame, text="Simplify tolerance, degrees\n(own file only, e.g. 0.001)").grid(
            row=b_row, column=0, sticky="w", **pad
        )
        self.simplify_tolerance = tk.StringVar()
        self.simplify_entry = ttk.Entry(self.breakdown_frame, textvariable=self.simplify_tolerance, width=10)
        self.simplify_entry.grid(row=b_row, column=1, sticky="w", **pad)
        b_row += 1

        self.load_fields_button = ttk.Button(
            self.breakdown_frame, text="Load available columns", command=self._on_load_fields
        )
        self.load_fields_button.grid(row=b_row, column=0, columnspan=2, sticky="w", **pad)

        ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
        row += 1

        ttk.Label(frm, text="Start date (YYYY-MM-DD, inclusive)").grid(row=row, column=0, sticky="w", **pad)
        self.start = tk.StringVar()
        ttk.Entry(frm, textvariable=self.start, width=14).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frm, text="End date (YYYY-MM-DD, exclusive)").grid(row=row, column=0, sticky="w", **pad)
        self.end = tk.StringVar()
        ttk.Entry(frm, textvariable=self.end, width=14).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frm, text="Frequency").grid(row=row, column=0, sticky="w", **pad)
        self.freq = tk.StringVar(value="monthly")
        ttk.Combobox(
            frm, textvariable=self.freq, values=list(VALID_FREQS), state="readonly", width=12
        ).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frm, text="Output CSV path").grid(row=row, column=0, sticky="w", **pad)
        self.out = tk.StringVar(value="out/nightlights.csv")
        ttk.Entry(frm, textvariable=self.out, width=40).grid(row=row, column=1, sticky="w", **pad)
        ttk.Button(frm, text="Save As...", command=self._on_browse_out).grid(row=row, column=2, **pad)
        row += 1

        self.chart = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="Also write a chart PNG", variable=self.chart).grid(
            row=row, column=0, columnspan=2, sticky="w", **pad
        )
        row += 1

        ttk.Label(
            frm, text="Chart units filter (comma-separated exact unit_name values, blank = all)"
        ).grid(row=row, column=0, sticky="w", **pad)
        self.chart_units = tk.StringVar()
        ttk.Entry(frm, textvariable=self.chart_units, width=40).grid(
            row=row, column=1, columnspan=2, sticky="w", **pad
        )
        row += 1

        ttk.Label(frm, text="Earth Engine cloud project ID (blank if your account doesn't need one)").grid(
            row=row, column=0, sticky="w", **pad
        )
        self.ee_project = tk.StringVar()
        ttk.Entry(frm, textvariable=self.ee_project, width=25).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        self.run_button = ttk.Button(frm, text="Run", command=self._on_run)
        self.run_button.grid(row=row, column=0, **pad)
        self.progress = ttk.Progressbar(frm, mode="indeterminate", length=200)
        self.progress.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        self.log = tk.Text(frm, height=14, width=100, state="disabled")
        self.log.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(row, weight=1)

    # ------------------------------------------------------------------
    # Dynamic enable/disable as the AOI source and granularity change
    # ------------------------------------------------------------------
    def _on_aoi_source_change(self) -> None:
        is_gaul = self.aoi_source.get() in ("iso3", "name")
        self.granularity_combo["values"] = GAUL_ADMIN_LEVEL_LABELS if is_gaul else ADMIN_LEVEL_LABELS
        if is_gaul and self.granularity.get() not in GAUL_ADMIN_LEVEL_LABELS:
            self.granularity.set(ADMIN_LEVEL_LABELS[0])
        self._on_granularity_change()

    def _on_granularity_change(self) -> None:
        level = self.breakdown_level()
        breakdown_on = bool(level)
        is_file = self.aoi_source.get() == "file"

        self._set_attribute_checkboxes_state(breakdown_on)
        self.load_fields_button.configure(state="normal" if breakdown_on else "disabled")
        # "normal" (editable), not "readonly" -- these need to stay typeable
        # even when "Load available columns" hasn't been clicked yet, or
        # failed (unreadable file, unsupported format). Matching the
        # wizard's own _prompt_single_field/_prompt_optional_field behavior:
        # validate against the known column list when there is one, but
        # never hard-block typing a name when there isn't.
        combo_state = "normal" if (breakdown_on and is_file) else "disabled"
        self.unit_name_combo.configure(state=combo_state)
        self.unit_id_combo.configure(state=combo_state)
        self.simplify_entry.configure(state="normal" if (breakdown_on and is_file) else "disabled")

    def breakdown_level(self) -> int:
        """0 for the whole-AOI option, 1-5 for Admin1-5."""
        try:
            return ADMIN_LEVEL_LABELS.index(self.granularity.get())
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # File/EE field lookups
    # ------------------------------------------------------------------
    def _on_browse_aoi_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose boundary file",
            filetypes=[("Boundary files", "*.geojson *.json *.shp *.gdb"), ("All files", "*.*")],
        )
        if not path:
            return
        self.aoi_file.set(path)
        self.aoi_source.set("file")
        self._on_aoi_source_change()
        # Auto-populate the breakdown columns (unit name/ID dropdowns, extra
        # attribute checkboxes) as soon as a file is chosen, rather than
        # making choosing a file and then clicking "Load available columns"
        # two separate steps. Listing a file's columns is just local file IO
        # (unlike the GAUL lookup below, it needs no admin level or Earth
        # Engine call), so there's no reason to wait for those.
        try:
            fields = list_file_fields(path)
        except Exception as e:  # noqa: BLE001 -- non-fatal; "Load available columns" remains as a retry
            messagebox.showerror("Load columns", f"Couldn't read {path}:\n{e}")
            return
        self._populate_field_widgets(fields)

    def _on_browse_out(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Output CSV path", defaultextension=".csv", filetypes=[("CSV", "*.csv")]
        )
        if path:
            self.out.set(path)

    def _on_load_fields(self) -> None:
        level = self.breakdown_level()
        if not level:
            messagebox.showinfo("Load columns", "Choose a granularity above Admin 0 first.")
            return
        try:
            fields = self._fetch_available_fields(level)
        except Exception as e:  # noqa: BLE001 -- surfaced to the user, not fatal to the GUI
            messagebox.showerror("Load columns", f"Couldn't load columns:\n{e}")
            return
        self._populate_field_widgets(fields)

    def _fetch_available_fields(self, level: int) -> list[str]:
        """The network/file-IO part of _on_load_fields, split out so it can
        be exercised directly (e.g. with a stubbed nightlight_tool) without
        touching any widgets."""
        source = self.aoi_source.get()
        if source == "file":
            path = self.aoi_file.get().strip()
            if not path:
                raise ValueError("Choose a boundary file first.")
            return list_file_fields(path)

        ee_project = self.ee_project.get().strip() or None
        if source == "iso3":
            iso3 = self.aoi_iso3.get().strip().upper()
            if not iso3:
                raise ValueError("Enter an ISO3 code first.")
            name = resolve_iso3_to_gaul_name(iso3, ee_project)
        else:
            name = self.aoi_name.get().strip()
            if not name:
                raise ValueError("Enter a country/admin name first.")
        return list_gaul_fields(name, f"admin{level}", ee_project)

    def _populate_field_widgets(self, fields: list[str]) -> None:
        self.unit_name_combo["values"] = fields
        self.unit_id_combo["values"] = fields

        for child in self.attributes_inner.winfo_children():
            child.destroy()
        self._attribute_vars = {}
        for f in fields:
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(self.attributes_inner, text=f, variable=var).pack(anchor="w")
            self._attribute_vars[f] = var
        self._set_attribute_checkboxes_state(bool(self.breakdown_level()))
        self.attributes_canvas.configure(scrollregion=self.attributes_canvas.bbox("all"))

    def _set_attribute_checkboxes_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for child in self.attributes_inner.winfo_children():
            child.configure(state=state)

    # ------------------------------------------------------------------
    # Collecting form values -> the pure argv-building function
    # ------------------------------------------------------------------
    def _collect_fields(self) -> dict:
        selected_attrs = [name for name, var in self._attribute_vars.items() if var.get()]
        return {
            "aoi_source": self.aoi_source.get(),
            "aoi_iso3": self.aoi_iso3.get(),
            "aoi_name": self.aoi_name.get(),
            "aoi_file": self.aoi_file.get(),
            "breakdown_level": self.breakdown_level(),
            "unit_name_field": self.unit_name_field.get(),
            "unit_id_field": self.unit_id_field.get(),
            "simplify_tolerance": self.simplify_tolerance.get() or None,
            "attributes": selected_attrs,
            "start": self.start.get(),
            "end": self.end.get(),
            "freq": self.freq.get(),
            "out": self.out.get(),
            "chart": self.chart.get(),
            "chart_units": self.chart_units.get(),
            "ee_project": self.ee_project.get(),
        }

    # ------------------------------------------------------------------
    # Run -- background thread so the window doesn't freeze during an
    # Earth Engine query, with output piped back into the log panel.
    # ------------------------------------------------------------------
    def _on_run(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        try:
            argv = build_argv_from_form(self._collect_fields())
        except ValueError as e:
            messagebox.showerror("Check your inputs", str(e))
            return

        self._append_log(f"Running: nightlight_tool.py {' '.join(argv)}\n\n")
        self.run_button.configure(state="disabled")
        self.progress.start(10)
        self._worker = threading.Thread(target=self._run_worker, args=(argv,), daemon=True)
        self._worker.start()

    def _run_worker(self, argv: list[str]) -> None:
        # sys.stdout/sys.stderr are process-wide, not per-thread, so this
        # swap also catches any print() that happens to run on the main
        # thread for the run's duration (there normally isn't any -- Tk's
        # own event loop doesn't print). Only one worker runs at a time (the
        # Run button stays disabled until this returns), which is what makes
        # this safe at all; a "Run all" / concurrent-runs feature would need
        # a real per-thread redirect instead of this swap.
        writer = _QueueWriter(self._log_queue)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        try:
            code = run_main(argv)
        except Exception as e:  # noqa: BLE001 -- reported in the log panel, not raised into Tk's loop
            self._log_queue.put(f"\nFailed: {e}\n")
            code = 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self._log_queue.put(f"__DONE__{code}\n")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                line = self._log_queue.get_nowait()
                if line.startswith("__DONE__"):
                    self._on_worker_done(int(line[len("__DONE__"):].strip()))
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self._poll_job = self.root.after(100, self._poll_log_queue)

    def _on_worker_done(self, code: int) -> None:
        self.progress.stop()
        self.run_button.configure(state="normal")
        self._append_log(f"\n{'Done.' if code == 0 else f'Exited with code {code}.'}\n")

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> None:
    root = tk.Tk()
    NightlightGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
