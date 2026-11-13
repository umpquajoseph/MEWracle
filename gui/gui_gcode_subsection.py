"""The Design window's saved subsection regions and their editor."""

import copy
import json
import math
import os
import re
import shutil
import subprocess
import threading
import uuid
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np

#: the shapes, as (stored value, label shown). The drawn one is named as the
#: Footprint tool names it.
_SHAPES = (("box", "Box"), ("circle", "Circle"), ("sphere", "Sphere"),
           ("outline", "Custom outline"))
_SHAPE_VALUE = {label: value for value, label in _SHAPES}
_SHAPE_LABEL = dict(_SHAPES)
#: the snap grids the outline editor opens with (mm)
_GRIDS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)
#: dimmed context: the full print outside the section
_DIM = (.62, .62, .62)
#: the coloured copy of the cut sits this much fatter than the context under
#: it, so the two never z-fight
_OVERLAY_GROW = 1.04
#: keys whose edits change nothing in the picture
_NO_PREVIEW = {"pores_x", "pores_y", "drag_mode"}


def _rect(w, h):
    return [[float("%.5g" % x), float("%.5g" % y)]
            for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2),
                         (w / 2, h / 2), (-w / 2, h / 2))]


class _GcodeSubsectionMixin:
    def _gce_persist_entries(self):
        entries = dict(super()._gce_persist_entries())
        if not hasattr(self, "gce_subsections"):
            self.gce_subsections = tk.StringVar(value="[]")
            self.gce_subsections.trace_add("write", self._subsections_changed)
        entries["gce.subsections"] = self.gce_subsections
        return entries

    def _subsections(self):
        if not hasattr(self, "gce_subsections"):
            self._gce_persist_entries()
        try:
            data = json.loads(self.gce_subsections.get() or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, tk.TclError):
            return []

    def _subsections_set(self, sections):
        self.gce_subsections.set(json.dumps(sections, separators=(",", ":")))

    def _subsections_changed(self, *_args):
        if getattr(self, "_gce_tree", None) is not None:
            self.root.after_idle(self._gce_refresh_anatomy)
        if getattr(self, "_gce_pv_panel", None) is not None:
            self.root.after_idle(self._gce_pv_show)

    def _gce_top_rows(self):
        rows = list(super()._gce_top_rows())
        rows.extend(("d:sub:" + s["id"], s["name"], "Subsection")
                    for s in self._subsections() if "id" in s and "name" in s)
        return rows

    def _subsection_from_row(self, iid):
        if not (iid or "").startswith("d:sub:"):
            return None
        return next((s for s in self._subsections()
                     if s.get("id") == iid[6:]), None)

    def _gce_row_hidden(self, iid):
        section = self._subsection_from_row(iid)
        return not section.get("enabled", True) if section else super()._gce_row_hidden(iid)

    def _gce_design_eye(self, iid):
        section = self._subsection_from_row(iid)
        if section is None:
            return super()._gce_design_eye(iid)
        sections = self._subsections()
        for s in sections:
            if s.get("id") == section["id"]:
                s["enabled"] = not s.get("enabled", True)
        self._subsections_set(sections)

    def _gce_design_open(self, iid):
        section = self._subsection_from_row(iid)
        if section is not None:
            self._subsection_open(section)
            return True
        return super()._gce_design_open(iid)

    def _gce_design_can_delete(self, iid):
        return self._subsection_from_row(iid) is not None or super()._gce_design_can_delete(iid)

    def _gce_design_delete(self, iid):
        section = self._subsection_from_row(iid)
        if section is None:
            return super()._gce_design_delete(iid)
        self._subsections_set([s for s in self._subsections()
                               if s.get("id") != section["id"]])

    def _gce_design_menu(self, menu, iid):
        section = self._subsection_from_row(iid)
        if section is None:
            return super()._gce_design_menu(menu, iid)
        menu.add_separator()
        menu.add_command(label="Edit subsection", command=lambda: self._subsection_open(section))
        menu.add_command(label="Delete subsection", command=lambda: self._gce_design_delete(iid))

    def _build_gce_design_groups(self, bar, ribbon_group):
        super()._build_gce_design_groups(bar, ribbon_group)
        self._subsection_tools = []
        for container in getattr(self, "_gce_c_sets", {}).values():
            group = ribbon_group(container, "Section")
            self._subsection_tools.append(self._gce_tool(
                group, "change", "subsection", "Subsection",
                "Mark a small region of the print, a few pores say, as a box, "
                "circle, sphere or outline; while you place it, the picture "
                "shows it in colour over the greyed print. Save STL as… then "
                "writes each region as its own STL instead "
                "of the whole scaffold: a file small enough to mesh or "
                "simulate. The g-code, and so the print, is not changed.",
                command=lambda: self._subsection_open()))
        self._gce_design_refresh()

    def _gce_design_refresh(self):
        super()._gce_design_refresh()
        for tool in getattr(self, "_subsection_tools", ()):
            tool.set_why(None)

    def _subsection_layers(self):
        return list(getattr(self, "_view_layers", ()) or ())

    def _subsection_gpu_cut(self, section, meta):
        """`(section to cut the centrelines with, planes)` for a box section.

        The export tubes a whole fibre and slices the MESH at the box, so a
        fibre is cut flat through its round body. The preview slices
        centrelines, which leaves the tube round past the box and square at
        its ends. For a box the picture is instead cut at the box's own
        planes on the graphics card, from centrelines kept a little longer
        than the box (a tube radius and a half) so the slice has fibre to
        show. Anything else (other shapes, a mandrel, a layer selection's
        wall-less sides) has no such cut and returns the section unchanged
        with no planes. VTK draws at most six clipping planes."""
        if section.get("shape") != "box" or section.get("mandrel_radius"):
            return section, None
        import mew_subsection
        try:
            normals, points = mew_subsection.clipping_planes(section)
        except (ValueError, KeyError):
            return section, None
        if len(normals) > 6:
            return section, None
        r = float(meta.get("tube_diameter", meta.get("fiber_diameter")) or 0.02) / 2.0
        radii = meta.get("point_radii")
        if radii is not None and len(radii):
            r = max(r, max(float(np.max(x)) for x in radii if len(x)))
        grow = 3.0 * r
        grown = dict(section)
        size = [float(x) for x in section["size"]]
        grown["size"] = [size[0] + grow, size[1] + grow,
                         size[2] + grow if section.get("z_mode", "height") == "height"
                         else size[2]]
        return grown, (normals, points)

    def _subsection_apply_planes(self, mapper, planes):
        """Cut a mapper's geometry at `planes` (None: no cut)."""
        import vtk
        mapper.RemoveAllClippingPlanes()
        if planes is None:
            return
        for normal, point in zip(*planes):
            plane = vtk.vtkPlane()
            plane.SetNormal(*[float(x) for x in normal])
            plane.SetOrigin(*[float(x) for x in point])
            mapper.AddClippingPlane(plane)

    def _subsection_caps(self, panel, section, pieces, radii, meta, grow=1.0):
        """The flat faces where the planes slice the tubes, as one actor.

        The clipped tubes are open where they are cut. The export closes each
        sliced fibre with `ClosedClipper` (a closed tube cut by the section's
        planes, capped); this runs the same cut on each fibre the box
        touches and keeps only the faces that lie ON a plane. They are drawn
        in the fibre colour, unclipped, beside the coloured tubes."""
        import vtk
        import mew_subsection
        import gui_preview
        from vtk.util import numpy_support as ns
        clipper = mew_subsection.ClosedClipper(section)
        normals, points = clipper.normals, clipper.points
        c = np.sum(normals * points, axis=1)
        sides = meta.get("tube_sides") or gui_preview.PREVIEW_TUBE_SIDES
        base = max(meta.get("tube_diameter", meta["fiber_diameter"]) / 2.0, 1e-5) * grow
        out_v, out_t, voff = [], [], 0
        for i, p in enumerate(pieces):
            p = np.asarray(p, dtype=np.float64)
            if len(p) < 2:
                continue
            r = None if radii is None else np.asarray(radii[i], dtype=np.float64) * grow
            rmax = base if r is None else float(np.max(r))
            if np.all(p @ normals.T - c >= rmax * 1.01):
                continue                     # clear of every plane: not sliced
            pts = vtk.vtkPoints()
            pts.SetData(ns.numpy_to_vtk(np.ascontiguousarray(p), deep=True))
            line = vtk.vtkCellArray()
            line.InsertNextCell(len(p))
            for k in range(len(p)):
                line.InsertCellPoint(k)
            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)
            pd.SetLines(line)
            tube = vtk.vtkTubeFilter()
            tube.SetInputData(pd)
            tube.SetRadius(base)
            if r is not None:
                arr = ns.numpy_to_vtk(np.ascontiguousarray(r), deep=True)
                arr.SetName("fiber_radius")
                pd.GetPointData().AddArray(arr)
                tube.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS,
                                            "fiber_radius")
                tube.SetVaryRadiusToVaryRadiusByAbsoluteScalar()
            tube.SetNumberOfSides(sides)
            tube.SetCapping(1)
            tri = vtk.vtkTriangleFilter()
            tri.SetInputConnection(tube.GetOutputPort())
            tri.Update()
            res = tri.GetOutput()
            if not res.GetNumberOfPolys():
                continue
            v = ns.vtk_to_numpy(res.GetPoints().GetData()).astype(np.float64)
            t = ns.vtk_to_numpy(res.GetPolys().GetConnectivityArray()).reshape(-1, 3).astype(np.int64)
            v, t = clipper.clip(v, t)
            if not len(t):
                continue
            on = np.abs(v @ normals.T - c) < 1e-6          # (vertices, planes)
            flat = np.any(on[t[:, 0]] & on[t[:, 1]] & on[t[:, 2]], axis=1)
            if not flat.any():
                continue
            out_v.append(v)
            out_t.append(t[flat] + voff)
            voff += len(v)
        if not out_t:
            return None
        v, t = np.concatenate(out_v), np.concatenate(out_t)
        used, inv = np.unique(t, return_inverse=True)
        pd = vtk.vtkPolyData()
        vp = vtk.vtkPoints()
        vp.SetData(ns.numpy_to_vtk(np.ascontiguousarray(v[used]), deep=True))
        pd.SetPoints(vp)
        cells = vtk.vtkCellArray()
        cells.SetData(ns.numpy_to_vtkIdTypeArray(np.arange(len(t) + 1, dtype=np.int64) * 3, deep=True),
                      ns.numpy_to_vtkIdTypeArray(inv.reshape(-1).astype(np.int64), deep=True))
        pd.SetPolys(cells)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        mapper.ScalarVisibilityOff()
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().DeepCopy(panel._actor.GetProperty())
        actor.GetProperty().SetColor(*self._sub_cap_color(panel))
        actor.GetProperty().SetOpacity(1.)
        actor.PickableOff()
        panel._ren.AddActor(actor)
        self._sub_actors.append(actor)
        return actor

    def _sub_cap_color(self, panel):
        return (panel._state or {}).get("color", (0.8, 0.25, 0.2))

    def _subsection_tube_frame(self, section):
        section = dict(section)
        if self._gce_pv_is_tube():
            ref = int(section.get("reference", 0))
            with self._gce_tube_scope(ref):
                design = self._mandrel_resolved_design()
                section["mandrel_radius"] = float(design.radius)
                section["mandrel_surface_z"] = float(self._mandrel_surface_z(design))
        return section

    def _subsection_body_paths(self, full):
        """The drawn polylines the default box is centred on: the fiber the
        play bar's layers lay, from the end of the first layer's first move
        to the last layer's end. The exit run after the last layer and the
        lead-in from wherever the head started are left out: on an opened
        file, where travels are written as G1 and drawn as fiber, they are
        the far points of the bounds, and the box landed off the scaffold.
        All of them when there are no layers to go by."""
        paths, meta = full
        allp = [np.asarray(p, np.float64) for p in paths if len(p)]
        L = getattr(self, "_view_layers", None) or []
        ptimes = (meta or {}).get("ptimes")
        if not L or ptimes is None or len(ptimes) != len(paths):
            return allp
        import preview_pick
        t0, t1 = L[0]["t0"], L[-1]["t1"]
        clock = (meta or {}).get("line_clock")
        if clock is not None:
            times = np.asarray(clock["times"], np.float64)
            j = int(np.searchsorted(np.asarray(clock["seg_line"]),
                                    L[0]["first"], side="left"))
            if j + 1 < len(times) and times[j + 1] < t1:
                t0 = float(times[j + 1])
        body = preview_pick.layer_polylines(paths, ptimes, t0, t1)
        body = [np.asarray(p, np.float64) for p in body if len(p)]
        return body or allp

    @staticmethod
    def _subsection_fiber_middle(paths):
        """(centre, lo, hi) in X/Y of where the fiber lies, by LENGTH: the
        length-weighted median and 5th/95th percentiles of the segments'
        midpoints. A file this app has no structure for (one from elsewhere,
        or saved on another computer) is one guessed layer, exit run and
        all, and its bounds' middle is off the scaffold; a run that is a
        small part of the fiber cannot move a median."""
        mids, w = [], []
        for p in paths:
            if len(p) < 2:
                continue
            d = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
            mids.append((p[1:, :2] + p[:-1, :2]) / 2)
            w.append(d)
        if not mids:
            return None
        mids, w = np.concatenate(mids), np.concatenate(w)
        if w.sum() <= 0:
            return None
        out = []
        for q in (.5, .05, .95):
            v = []
            for ax in (0, 1):
                o = np.argsort(mids[:, ax])
                cw = np.cumsum(w[o])
                v.append(float(mids[o[min(int(np.searchsorted(
                    cw, q * cw[-1])), len(o) - 1)], ax]))
            out.append(np.array(v))
        return out

    def _subsection_default(self):
        full = getattr(self, "_gce_pv_full", None)
        if full is None or not full[0]:
            raise ValueError("Wait for the scaffold preview before adding a subsection.")
        body = self._subsection_body_paths(full)
        pts = np.concatenate(body)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        span = hi - lo
        dia = float((full[1] or {}).get("fiber_diameter", .02))
        size = np.maximum(span * .35, max(dia * 4, .001))
        centre = (lo + hi) / 2
        ref = self._gce_d_target()
        if not isinstance(ref, int) or not 0 <= ref < self._gce_scaffold_count():
            ref = 0
        tube = self._gce_pv_is_tube()
        if tube:
            try:
                with self._gce_page_scope():
                    with self._gce_tube_scope(ref):
                        design = self._mandrel_resolved_design()
                        layout = self._mandrel_axial_layout(design)
                        radius = float(design.radius) + self._gce_start_out(ref)
                        layers = self._mandrel_layers()
                # The co-rotating preview uses the same axial frame as its
                # cached fibres. Pick its middle and the top of the winding.
                centre = np.array([layout.print_start_x + design.length / 2, 0.,
                                   radius + max(0, layers - 1) * dia / 2])
                size = np.array([max(float(design.length) * .35, dia * 4),
                                 max(radius * .7, dia * 4), max(radius * .3, dia * 4)])
            except (AttributeError, ValueError, tk.TclError):
                pass
        elif (getattr(self, "_gce_doc", None) or {}).get("source") != self._GCE_FILE:
            # Start X/Y are g-code numbers, measured from zero; the section
            # lives in the picture's frame, `zx, zy` away
            zx, zy = self._subsection_zero_xy()
            try:
                v = self.gce_scaf_vars[ref]
                x, y = float(v["start_x"].get()), float(v["start_y"].get())
                w, h = float(v["width"].get()), float(v["height"].get())
                centre[:2] = x + w / 2 + zx, y + h / 2 + zy
                size[:2] = max(w * .35, dia * 4), max(h * .35, dia * 4)
            except (KeyError, ValueError, tk.TclError):
                pass
            for i, values in enumerate(getattr(self, "gce_scaf_vars", ())):
                try:
                    x, y = float(values["start_x"].get()), float(values["start_y"].get())
                    w, h = float(values["width"].get()), float(values["height"].get())
                    x, y = x + zx, y + zy
                    if x <= centre[0] <= x + w and y <= centre[1] <= y + h:
                        ref = i
                        break
                except (KeyError, ValueError, tk.TclError):
                    pass
        else:
            # an opened file has no Start X/Y: where its fiber lies
            mid = self._subsection_fiber_middle(body)
            if mid is not None:
                centre[:2] = mid[0]
                size[:2] = np.maximum((mid[2] - mid[1]) * .35, dia * 4)
        return dict(id=uuid.uuid4().hex, name="Subsection %d" % (len(self._subsections()) + 1),
                    enabled=True, shape="box", snap_surface=bool(tube), center=centre.tolist(), size=size.tolist(),
                    rotation=0., z_mode="height", layer_start=1,
                    layer_end=max(1, len(self._subsection_layers())), reference=ref,
                    outline_mm=_rect(float(size[0]), float(size[1])))

    def _subsection_grid(self, size):
        """A snap grid giving the outline editor about eight cells across."""
        want = max(float(size[0]), float(size[1]), 1e-6) / 8.0
        return min(_GRIDS, key=lambda g: abs(math.log(g / want)))

    def _subsection_outline_pairs(self, section):
        import mew_subsection
        try:
            return [(float(x), float(y)) for x, y in mew_subsection.outline_points(section)]
        except (ValueError, KeyError, TypeError):
            return [tuple(p) for p in _rect(float(section["size"][0]), float(section["size"][1]))]

    def _subsection_open(self, section=None):
        try:
            section = copy.deepcopy(section or self._subsection_default())
        except ValueError as exc:
            self._refuse(str(exc))
            return
        self._subsection_close()
        host, body = self._gce_host_open("Subsection")
        size = section["size"]
        pairs = self._subsection_outline_pairs(section)
        # the outline editor is the Footprint tool's own (gui_scaffold
        # `_build_shape_editor`), driven here by its own small set of vars
        ov = dict(shape=tk.StringVar(value="custom"),
                  shape_polygon=tk.StringVar(value="; ".join("%g,%g" % p for p in pairs)),
                  shape_grid=tk.StringVar(value="%g" % self._subsection_grid(size)),
                  shape_editmode=tk.StringVar(value="add"), shape_view=None)
        draft = self._sub_draft = dict(section=section, host=host, vars={}, rows={}, drag=None,
                                       outline=ov, syncing=True, rendered=None, nrow=0)
        vars_, rows = draft["vars"], draft["rows"]
        body.columnconfigure(1, weight=1)

        def row(label, key, value, choices=None):
            r = draft["nrow"]
            draft["nrow"] += 1
            lab = ttk.Label(body, text=label)
            lab.grid(row=r, column=0, sticky="w", padx=(0, 8))
            v = tk.StringVar(value=str(value))
            w = (ttk.Combobox(body, textvariable=v, values=choices, state="readonly", width=22)
                 if choices else ttk.Entry(body, textvariable=v, width=24))
            w.grid(row=r, column=1, sticky="ew", pady=2)
            vars_[key], rows[key] = v, (lab, w)
            return v

        row("Name", "name", section["name"])
        row("Shape", "shape", _SHAPE_LABEL.get(section["shape"], "Box"), [l for _v, l in _SHAPES])
        draft["R"] = self._subsection_radius(section)
        draft["snap_on"] = bool(draft["R"] and section.get("snap_surface")
                                and section.get("shape") != "sphere")
        shown = self._subsection_from_zero(section["center"], -1)
        if draft["snap_on"]:
            shown = self._subsection_to_surface(draft["R"], shown)
        for i, axis in enumerate("XYZ"):
            row("Center %s (mm)" % axis, "c" + axis, "%.6g" % float(shown[i]))
        self._subsection_centre_labels()
        for i, axis in enumerate("XYZ"):
            row("Size %s (mm)" % axis, "s" + axis, "%.6g" % float(section["size"][i]))
        # the custom outline: shown only while that shape is chosen
        editor = draft["editor"] = ttk.LabelFrame(body, text="Custom outline")
        editor.grid(row=draft["nrow"], column=0, columnspan=2, sticky="ew", pady=(6, 4))
        draft["nrow"] += 1
        ttk.Label(editor, text="Draw a convex outline about the center. Click to add a point, "
                  "drag one to move it, switch to Remove to delete.",
                  wraplength=280, justify="left").grid(row=0, column=0, sticky="w", padx=4, pady=(2, 0))
        inner = self._build_shape_editor(editor, ov, stacked=True)
        inner.grid(row=1, column=0, sticky="nw", padx=4, pady=4)
        row("Rotation (deg)", "rotation", section.get("rotation", 0))
        if self._gce_pv_is_tube():
            snap = draft["snap"] = tk.BooleanVar(value=bool(draft["snap_on"]))
            cb = draft["snap_cb"] = ttk.Checkbutton(body, text="Snap to surface", variable=snap)
            cb.grid(row=draft["nrow"], column=0, columnspan=2, sticky="w", pady=2)
            draft["nrow"] += 1
            from gui_tips import add_tip
            add_tip(cb, "Lay the section's footprint on the mandrel's surface instead of "
                        "leaving it a flat box through the tube. Its X runs along the mandrel and "
                        "its Y round it, in millimeters of arc. The center is then placed "
                        "along the mandrel, round it and by its lift off the surface: dragging "
                        "X and Y slides it over the surface, and only Z lifts it off.")
            snap.trace_add("write", lambda *_a: self._subsection_snap_toggled())
        row("Height selection", "z_mode", section.get("z_mode", "height"), ("height", "layers"))
        layers = self._subsection_layers()
        labels = ["%d (%s, layer %d)" % (
                    i + 1,
                    "Stabilization scaffold" if x["name"].lower() == "base layers" else x["name"],
                    x["layer"])
                  for i, x in enumerate(layers)] or ["1 (print)"]
        start = min(max(int(section.get("layer_start", 1)), 1), len(labels))
        end = min(max(int(section.get("layer_end", len(labels))), 1), len(labels))
        row("First layer", "layer_start", labels[start - 1], labels)
        row("Last layer", "layer_end", labels[end - 1], labels)
        refs = ["Scaffold %d" % (i + 1) for i in range(self._gce_scaffold_count())] or ["Scaffold 1"]
        ref = min(max(int(section.get("reference", 0)), 0), len(refs) - 1)
        row("Pore reference", "reference", refs[ref], refs)
        tube = self._gce_pv_is_tube()
        row("Pores along axis" if tube else "Pores X", "pores_x", 3)
        row("Pores around tube" if tube else "Pores Y", "pores_y", 3)
        draft["pores_btn"] = ttk.Button(body, text="Set size from pores", command=self._subsection_pores)
        draft["pores_btn"].grid(row=draft["nrow"], column=0, columnspan=2, sticky="w", pady=3)
        draft["nrow"] += 1
        row("Drag handles", "drag_mode", "Move", ("Move", "Resize"))
        draft["status"] = ttk.Label(host, text="", wraplength=300, justify="left")
        draft["status"].grid(row=3, column=0, sticky="w", pady=(4, 0))
        buttons = ttk.Frame(host)
        buttons.grid(row=4, column=0, sticky="w", pady=8)
        ttk.Button(buttons, text="Done", command=self._subsection_done).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self._subsection_close).pack(side="left", padx=5)
        self._gce_host_keys(host, self._subsection_done, self._subsection_close)
        self._render_shape_table(ov)
        ov["shape_polygon"].trace_add("write", lambda *_a: self._subsection_outline_changed())
        draft["syncing"] = False
        self._subsection_apply_shape()
        for key, v in vars_.items():
            v.trace_add("write", lambda *_a, key=key: self._subsection_field_changed(key))
        if any(x.get("enabled", True) for x in self._subsections()):
            # an enabled section left the picture cut down to itself: put the
            # whole print back (dimmed, with the cut over it) to edit against
            self._gce_pv_show()
        self._subsection_draft_changed()
        self._subsection_request_exact()

    def _subsection_radius(self, section):
        """The mandrel's radius under this section's scaffold, or None off a
        4-axis design."""
        if not self._gce_pv_is_tube():
            return None
        try:
            return float(self._subsection_tube_frame(section).get("mandrel_radius") or 0) or None
        except (ValueError, KeyError, AttributeError, tk.TclError):
            return None

    @staticmethod
    def _subsection_to_surface(radius, c):
        """A centre in the rod's frame -> (along the rod, millimetres of arc
        round it from the top, lift off the surface)."""
        rc = math.hypot(c[1], c[2])
        return c[0], radius * math.atan2(c[1], c[2]), rc - radius

    @staticmethod
    def _subsection_from_surface(radius, p):
        """The inverse of `_subsection_to_surface`."""
        rc = radius + p[2]
        if rc <= 0:
            raise ValueError("Lift cannot take the center through the mandrel's axis.")
        phi = p[1] / radius
        return p[0], rc * math.sin(phi), rc * math.cos(phi)

    def _subsection_centre_labels(self):
        d = self._sub_draft
        for axis, free, snapped in (("X", "Center X (mm)", "Along mandrel (mm)"),
                                    ("Y", "Center Y (mm)", "Around mandrel (mm arc)"),
                                    ("Z", "Center Z (mm)", "Lift (mm)")):
            d["rows"]["c" + axis][0].configure(text=snapped if d.get("snap_on") else free)

    def _subsection_centre_xyz(self):
        """The centre the form describes, in the rod's frame."""
        d = self._sub_draft
        snapped = bool(d.get("snap_on"))
        names = (("Along mandrel", "Around mandrel", "Lift") if snapped
                 else ("Center X", "Center Y", "Center Z"))
        c = tuple(self._subsection_number("c" + a, n) for a, n in zip("XYZ", names))
        if snapped:
            c = self._subsection_from_surface(d["R"], c)
        return self._subsection_from_zero(c, 1)

    def _subsection_zero_xy(self):
        """Where g-code zero's X/Y sit in the picture: the picture is drawn
        on the plate or the rod, the text and Start X/Y are measured from
        zero. A tube's zero moves along the rod only (its Y is 0)."""
        try:
            shift = self._gce_pv_options().get("preview_shift") or (0.0, 0.0)
            return (float(shift[0]), float(shift[1]))
        except (AttributeError, ValueError, TypeError, IndexError, tk.TclError):
            return (0.0, 0.0)

    def _subsection_from_zero(self, c, sign):
        """A section's centre between the form's terms, X/Y measured from
        g-code zero as Start X/Y and the text are (sign -1: picture ->
        form), and the picture's frame it is kept and drawn in (sign 1)."""
        zx, zy = self._subsection_zero_xy()
        c = tuple(float(v) for v in c)
        return (c[0] + sign * zx, c[1] + sign * zy) + c[2:]

    def _subsection_set_centre(self, c):
        """Show a centre given in the rod's frame, in the form's own terms."""
        d = self._sub_draft
        shown = self._subsection_from_zero(c, -1)
        if d.get("snap_on"):
            shown = self._subsection_to_surface(d["R"], shown)
        for axis, value in zip("XYZ", shown):
            d["vars"]["c" + axis].set("%.5g" % value)

    def _subsection_snap_toggled(self):
        d = getattr(self, "_sub_draft", None)
        if d is None or d.get("syncing"):
            return
        on = bool(d["snap"].get())
        if on == bool(d.get("snap_on")):
            return
        try:
            c = self._subsection_centre_xyz()
        except ValueError:
            c = None
        d["syncing"] = True
        try:
            d["snap_on"] = on
            if c is not None:
                self._subsection_set_centre(c)
            self._subsection_centre_labels()
        finally:
            d["syncing"] = False
        self._subsection_draft_changed()

    #: which of (along, round, lift) each gimbal handle moves a snapped section by
    _SNAP_HANDLE = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1), "xy": (1, 1, 0),
                    "zx": (1, 0, 1), "yz": (0, 1, 1), "center": (1, 1, 1)}

    def _subsection_snap_move(self, c, delta, handle):
        """A snapped centre moved by a gimbal drag (a world delta): along the
        rod, round it, and out from it. The XY handles slide it over the
        surface and never lift it; the Z handle lifts it and never slides."""
        use = self._SNAP_HANDLE.get(handle, (1, 1, 1))
        rc, phi = math.hypot(c[1], c[2]), math.atan2(c[1], c[2])
        around = delta[1] * math.cos(phi) - delta[2] * math.sin(phi)
        out = delta[1] * math.sin(phi) + delta[2] * math.cos(phi)
        phi += use[1] * around / max(rc, 1e-9)
        rc = max(rc + use[2] * out, 1e-6)
        return [c[0] + use[0] * delta[0], rc * math.sin(phi), rc * math.cos(phi)]

    def _subsection_shape(self):
        d = getattr(self, "_sub_draft", None)
        return _SHAPE_VALUE.get(d["vars"]["shape"].get(), "box") if d else "box"

    def _subsection_apply_shape(self):
        """Make the form say what the chosen shape uses: the sizes the shape
        drives from another are greyed (a sphere's Y and Z follow its
        diameter, a circle's Y follows X, a drawn outline sets X and Y
        itself), the outline editor shows only for the drawn shape, the layer
        rows only for a layer selection, the pore rows only for a box."""
        d = self._sub_draft
        if d is None:
            return
        v, rows = d["vars"], d["rows"]
        shape = self._subsection_shape()
        layered = v["z_mode"].get() == "layers"
        was, d["syncing"] = d["syncing"], True
        try:
            if shape == "sphere":
                v["sY"].set(v["sX"].get())
                v["sZ"].set(v["sX"].get())
            elif shape == "circle":
                v["sY"].set(v["sX"].get())
            elif shape == "outline":
                self._subsection_bbox_to_size()
            driven = {"box": (), "circle": ("sY",), "sphere": ("sY", "sZ"),
                      "outline": ("sX", "sY")}[shape]

            def enable(key, on):
                w = rows[key][1]
                w.configure(state=("readonly" if isinstance(w, ttk.Combobox) else "normal")
                            if on else "disabled")
            for key in ("sX", "sY", "sZ"):
                on = key not in driven and not (key == "sZ" and layered and shape != "sphere")
                enable(key, on)
            enable("rotation", shape != "sphere")
            if d.get("snap_cb") is not None:
                d["snap_cb"].configure(state="disabled" if shape == "sphere" else "normal")
            for key in ("layer_start", "layer_end"):
                enable(key, layered)
            for key in ("reference", "pores_x", "pores_y"):
                enable(key, shape == "box")
            d["pores_btn"].configure(state="normal" if shape == "box" else "disabled")
            rows["sX"][0].configure(text="Diameter (mm)" if shape in ("circle", "sphere")
                                    else "Size X (mm)")
            rows["sZ"][0].configure(text="Height (mm)" if shape in ("circle", "outline")
                                    else "Size Z (mm)")
            if shape == "outline":
                d["editor"].grid()
            else:
                d["editor"].grid_remove()
            self._gce_layout_soon()
        finally:
            d["syncing"] = was
        if shape == "sphere" and d.get("snap") is not None and d["snap"].get():
            d["snap"].set(False)

    def _subsection_pairs(self):
        d = self._sub_draft
        pairs, err = self._parse_linear_pattern(d["outline"]["shape_polygon"].get())
        if pairs is None:
            raise ValueError("Outline: " + err)
        return pairs

    def _subsection_bbox_to_size(self):
        """A drawn outline's X and Y size are its bounding box."""
        d = self._sub_draft
        try:
            pairs = self._subsection_pairs()
        except ValueError:
            return
        if len(pairs) < 2:
            return
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        for key, span in (("sX", max(xs) - min(xs)), ("sY", max(ys) - min(ys))):
            if span > 0:
                d["vars"][key].set("%.6g" % span)

    def _subsection_outline_changed(self):
        d = getattr(self, "_sub_draft", None)
        if d is None:
            return
        self._redraw_shape_canvas(d["outline"])
        if self._subsection_shape() == "outline" and not d["syncing"]:
            d["syncing"] = True
            try:
                self._subsection_bbox_to_size()
            finally:
                d["syncing"] = False
            self._subsection_draft_changed()

    def _subsection_status(self, text="", error=True):
        d = getattr(self, "_sub_draft", None)
        if d is not None and d.get("status") is not None:
            try:
                d["status"].configure(text=text, foreground="#d62728" if error and text else "")
            except tk.TclError:
                pass

    def _subsection_pores(self):
        draft = self._sub_draft
        v = draft["vars"]
        try:
            index = int(v["reference"].get().split()[-1]) - 1
            values = self.gce_scaf_vars[index]
            nx, ny = float(v["pores_x"].get()), float(v["pores_y"].get())
            if min(nx, ny) <= 0:
                raise ValueError("Pore counts must be positive.")
            if self._gce_pv_is_tube():
                import mew_tube
                with self._gce_tube_scope(index):
                    design = self._mandrel_resolved_design()
                    radius = float(design.radius) + self._gce_start_out(index)
                    layers = self._mandrel_layers()
                    fiber = self._mandrel_fiber_diameter()
                if hasattr(design, "cell_w"):
                    axial, around = float(design.cell_w), float(design.cell_h)
                else:
                    around = mew_tube.pore_arc_length(design.radius, design.pivot_angle)
                    axial = 2 * mew_tube.pore_triangle_height(around, design.omega_deg)
                angle = min(2 * math.pi, ny * around / radius)
                outer = radius + max(0, layers - 1) * fiber + fiber / 2
                width = 2 * outer * math.sin(min(angle / 2, math.pi / 2))
                depth = 2 * max(outer - radius * math.cos(angle / 2), fiber * 2)
                v["sX"].set("%.6g" % (nx * axial))
                v["sY"].set("%.6g" % width)
                v["sZ"].set("%.6g" % depth)
                return
            spacing = float(values["stepover"].get())
            if values["angle_pattern_on"].get():
                import scaffold_gcode
                angles = scaffold_gcode.parse_angle_pattern(values["angle_pattern"].get())
                steps = scaffold_gcode.parse_layer_value_list(values["angle_stepover_pattern"].get())
                if angles and steps and steps[0] is not None:
                    spacing = steps[0]
            overrides = json.loads(values["layer_overrides"].get() or "{}")
            first = overrides.get("1", {})
            spacing = float(first.get("stepover", spacing))
            if min(spacing, nx, ny) <= 0:
                raise ValueError("Pore counts and spacing must be positive.")
            sx = sy = spacing
            end = first.get("stepover_end", values["stepover_end"].get())
            if str(end).strip():
                # a pore size graded across the layer: each pore count is
                # sized from the spacing where the section sits, along its
                # own X and Y (scaffold_gcode.local_stepover)
                import scaffold_gcode
                w, h = float(values["width"].get()), float(values["height"].get())
                cx, cy, _cz = self._subsection_from_zero(
                    self._subsection_centre_xyz(), -1)
                dx = cx - (float(values["start_x"].get()) + w / 2)
                dy = cy - (float(values["start_y"].get()) + h / 2)
                rot = float(v["rotation"].get() or 0)
                kw = dict(footprint_angle=float(values["footprint_angle"].get() or 0),
                          shape=values["shape"].get(),
                          shape_sides=int(float(values["shape_sides"].get() or 64)))
                if kw["shape"] == "custom":
                    pairs, _e = self._parse_linear_pattern(values["shape_polygon"].get())
                    kw["footprint_polygon"] = pairs or None
                grade = float(first.get("grade_angle", values["grade_angle"].get() or 0))
                sx, sy = (scaffold_gcode.local_stepover(
                              spacing, float(end), grade, w, h, dx, dy, rot + a, **kw)
                          for a in (0.0, 90.0))
            v["sX"].set("%.6g" % (nx * sx))
            v["sY"].set("%.6g" % (ny * sy))
        except (ValueError, KeyError, IndexError, tk.TclError) as exc:
            self._refuse(str(exc))

    def _subsection_number(self, key, label, positive=False):
        try:
            x = float(self._sub_draft["vars"][key].get())
        except (ValueError, tk.TclError):
            raise ValueError("%s must be a number." % label)
        if not math.isfinite(x) or (positive and x <= 0):
            raise ValueError("%s must be %s." % (label, "a positive number" if positive else "finite"))
        return x

    def _subsection_value(self):
        """The section the form describes, validated; ValueError says what to fix."""
        import mew_subsection
        d = self._sub_draft
        v = d["vars"]
        s = copy.deepcopy(d["section"])
        shape = self._subsection_shape()
        s.update(name=v["name"].get().strip(), shape=shape,
                 center=list(self._subsection_centre_xyz()),
                 size=[self._subsection_number("s" + x, "Size " + x, positive=True) for x in "XYZ"],
                 rotation=self._subsection_number("rotation", "Rotation"), z_mode=v["z_mode"].get())
        if d.get("snap") is not None:
            s["snap_surface"] = bool(d["snap"].get())
        try:
            s.update(layer_start=int(v["layer_start"].get().split()[0]),
                     layer_end=int(v["layer_end"].get().split()[0]),
                     reference=int(v["reference"].get().split()[-1]) - 1)
        except (ValueError, IndexError):
            raise ValueError("Choose the first and last layer and the pore reference.")
        try:
            pairs = self._subsection_pairs()
        except ValueError:
            pairs = []
        if len(pairs) >= 3:
            s["outline_mm"] = [[x, y] for x, y in pairs]
            s.pop("outline", None)
        elif shape == "outline":
            raise ValueError("Draw an outline with at least three points.")
        if shape == "outline":
            xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
            s["size"][0], s["size"][1] = max(xs) - min(xs), max(ys) - min(ys)
            if min(s["size"][:2]) <= 0:
                raise ValueError("The outline has no area.")
        if not s["name"] or s["layer_start"] > s["layer_end"]:
            raise ValueError("Enter a name and a first layer no later than the last.")
        if any(x.get("id") != s["id"] and x.get("name", "").casefold() == s["name"].casefold()
               for x in self._subsections()):
            raise ValueError("Give each subsection a different name.")
        # a snapped section is checked in the tube's frame (its reach round the rod)
        mew_subsection.clipping_planes(self._subsection_tube_frame(s))
        return s

    def _subsection_done(self):
        try:
            s = self._subsection_value()
        except (ValueError, KeyError, tk.TclError) as exc:
            self._refuse(str(exc))
            return
        sections = [x for x in self._subsections() if x.get("id") != s["id"]]
        sections.append(s)
        self._subsections_set(sections)        # its trace shows the picture, once
        self._subsection_close(show=False)

    def _subsection_close(self, show=True):
        d = getattr(self, "_sub_draft", None)
        if d is None:
            return
        after = getattr(self, "_sub_after", None)
        if after:
            self.root.after_cancel(after)
            self._sub_after = None
        self._sub_draft = None
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is not None:
            panel.show_gimbal(None)
            panel.on_gimbal = None
        if getattr(self, "_gce_form", None) is d["host"]:
            self._gce_host_close()
        if show:
            self._gce_pv_show()

    def _gce_host_close(self):
        draft = getattr(self, "_sub_draft", None)
        if draft is not None and getattr(self, "_gce_form", None) is draft["host"]:
            self._sub_draft = None
            panel = getattr(self, "_gce_pv_panel", None)
            if panel is not None:
                panel.show_gimbal(None)
                panel.on_gimbal = None
            self.root.after_idle(self._gce_pv_show)
        return super()._gce_host_close()

    def _subsection_draft_changed(self):
        draft = getattr(self, "_sub_draft", None)
        if draft is None:
            return
        after = getattr(self, "_sub_after", None)
        if after:
            self.root.after_cancel(after)
            self._sub_after = None
        if draft.get("drag") is not None:
            self._subsection_drag_view()
            return
        self._sub_after = self.root.after(150, self._subsection_render)

    def _subsection_drag_view(self):
        """Move the outline immediately, and re-cut the fibres inside it as
        plain coloured lines so the highlight follows the cursor. The tubes,
        times and colours of the full cut are rebuilt on release."""
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is None or panel._ren is None:
            return
        try:
            section = self._subsection_value()
        except (ValueError, KeyError, tk.TclError):
            return
        self._subsection_unwire(panel)
        for actor in getattr(self, "_sub_pieces", ()):
            actor.SetVisibility(0)
        self._subsection_wireframe(section, panel)
        self._subsection_drag_cut(section, panel)
        panel._render_soon()

    def _subsection_cut_later(self):
        self._sub_cut_later = None
        draft = getattr(self, "_sub_draft", None)
        if draft is not None and draft.get("drag") is not None:
            self._subsection_drag_view()

    def _subsection_drag_cut(self, section, panel):
        """The cut as one line actor, rebuilt at most as fast as it can be
        cut: the next cut waits for twice the last one's cost, so the
        outline never lags behind the cursor."""
        import time
        now = time.perf_counter()
        if now < getattr(self, "_sub_cut_ready", 0.):
            # too soon: cut once more when the wait is over, so a drag that
            # stops still shows the cut at the place it stopped
            if getattr(self, "_sub_cut_later", None) is None:
                self._sub_cut_later = self.root.after(
                    int(1000 * (self._sub_cut_ready - now)) + 5, self._subsection_cut_later)
            return
        state = panel._state or {}
        paths = state.get("paths") or []
        meta = state.get("meta") or {}
        if not paths or panel._actor is None:
            return
        try:
            import vtk
            import mew_subsection
            from vtk.util import numpy_support as ns
            sec = self._subsection_tube_frame(section)
            pieces, _t, _r = mew_subsection.section_pieces(
                sec, paths, meta.get("ptimes"), None,
                self._subsection_layers(), meta.get("command_ptimes"))
        except (ValueError, KeyError, AttributeError, tk.TclError):
            return
        actor = getattr(self, "_sub_drag_actor", None)
        if actor is None or actor not in getattr(self, "_sub_actors", ()):
            mapper = vtk.vtkPolyDataMapper()
            mapper.ScalarVisibilityOff()
            mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-3., -3.)
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*state.get("color", (0.8, 0.25, 0.2)))
            actor.GetProperty().SetLineWidth(3.)
            actor.GetProperty().SetLighting(False)
            actor.PickableOff()
            if getattr(self, "_sub_resolve", None) is None:
                self._sub_resolve = vtk.vtkMapper.GetResolveCoincidentTopology()
                vtk.vtkMapper.SetResolveCoincidentTopologyToPolygonOffset()
            panel._ren.AddActor(actor)
            self._sub_actors.append(actor)
            self._sub_drag_actor = actor
        pd = vtk.vtkPolyData()
        if pieces:
            pts = np.vstack(pieces)
            sizes = np.fromiter((len(p) for p in pieces), np.int64, len(pieces))
            ends = np.cumsum(sizes)
            starts = ends - sizes
            conn = np.arange(len(pts), dtype=np.int64)
            offsets = np.concatenate(([0], ends)).astype(np.int64)
            cells = vtk.vtkCellArray()
            cells.SetData(ns.numpy_to_vtkIdTypeArray(offsets, deep=True),
                          ns.numpy_to_vtkIdTypeArray(conn, deep=True))
            vp = vtk.vtkPoints()
            vp.SetData(ns.numpy_to_vtk(np.ascontiguousarray(pts, np.float64), deep=True))
            pd.SetPoints(vp)
            pd.SetLines(cells)
        actor.GetMapper().SetInputData(pd)
        actor.SetVisibility(1)
        self._sub_cut_ready = time.perf_counter() + 2. * (time.perf_counter() - now)

    def _subsection_field_changed(self, key):
        d = getattr(self, "_sub_draft", None)
        if d is None or d.get("syncing") or key in _NO_PREVIEW:
            return
        if key in ("shape", "sX", "z_mode"):
            self._subsection_apply_shape()
        self._subsection_draft_changed()

    def _subsection_drag(self, delta, done):
        d = getattr(self, "_sub_draft", None)
        if d is None:
            return
        v = d["vars"]
        shape = self._subsection_shape()
        d["syncing"] = True
        try:
            if d["drag"] is None:
                gd = getattr(getattr(self, "_gce_pv_panel", None), "_gdrag", None)
                d["drag_handle"] = gd[0] if gd else "center"
                d["drag"] = (list(self._subsection_centre_xyz()),
                             [float(v["s" + x].get()) for x in "XYZ"],
                             float(v["rotation"].get()),
                             self._subsection_pairs() if shape == "outline" else None)
            center, size, angle, pairs = d["drag"]
            if v["drag_mode"].get() == "Resize":
                if shape == "sphere":
                    i = max(range(3), key=lambda n: abs(delta[n]))
                    v["sX"].set("%.5g" % max(.000001, size[0] + 2 * delta[i]))
                elif shape == "circle":
                    i = max(range(2), key=lambda n: abs(delta[n]))
                    v["sX"].set("%.5g" % max(.000001, size[0] + 2 * delta[i]))
                    v["sZ"].set("%.5g" % max(.000001, size[2] + 2 * delta[2]))
                elif shape == "outline":
                    fx = max(.001, (size[0] + 2 * delta[0]) / size[0])
                    fy = max(.001, (size[1] + 2 * delta[1]) / size[1])
                    d["outline"]["shape_polygon"].set(
                        "; ".join("%.5g,%.5g" % (x * fx, y * fy) for x, y in pairs))
                    v["sZ"].set("%.5g" % max(.000001, size[2] + 2 * delta[2]))
                else:
                    for i, axis in enumerate("XYZ"):
                        v["s" + axis].set("%.5g" % max(.000001, size[i] + 2 * delta[i]))
                if shape in ("sphere", "circle"):
                    self._subsection_apply_shape()
                elif shape == "outline":
                    self._subsection_bbox_to_size()
            else:
                if d.get("snap_on"):
                    moved = self._subsection_snap_move(center, delta, d.get("drag_handle"))
                else:
                    moved = [center[i] + delta[i] for i in range(3)]
                self._subsection_set_centre(moved)
                gimbal = getattr(getattr(self, "_gce_pv_panel", None), "_gimbal", None)
                if d.get("snap_on") and gimbal is not None:
                    gimbal.show(tuple(moved))      # it rides the surface with the box
            v["rotation"].set("%.5g" % (angle + delta[3]))
            if done:
                d["drag"] = None
        except (ValueError, tk.TclError):
            d["drag"] = None
        finally:
            d["syncing"] = False
        self._subsection_draft_changed()

    # ------------------------------------------------------------------ #
    # The picture. Two states, neither of which hides the print from the
    # click: the Design view draws the SAME fibres as ordinary fibres
    # (`_gce_pv_clip` hands the cut centrelines to the panel, so colours, the
    # print clock, tubes and click-to-select all work on them), and while the
    # tool is open the whole print stays up, dimmed, with the cut drawn
    # coloured over it.
    # ------------------------------------------------------------------ #
    def _gce_pv_show(self):
        panel = getattr(self, "_gce_pv_panel", None)
        self._subsection_clear(panel)
        self._sub_empty = False
        super()._gce_pv_show()
        if panel is not None and panel._actor is not None:
            self._subsection_apply_planes(panel._mapper, getattr(self, "_sub_gpu_planes", None))
            self._subsection_design_caps(panel)
        draft = getattr(self, "_sub_draft", None)
        if draft is not None:
            draft["rendered"] = None
        self._subsection_render()
        if getattr(self, "_sub_empty", False) and panel is not None:
            panel.set_status("No fiber lies inside the subsection.", error=True)

    def _gce_pv_clip(self, shown, meta):
        """Only the fibres inside the enabled subsections, each cut at the
        boundary, with their print clock and radii cut to match. While the
        tool is open, or the simulation plays, the whole print."""
        shown, meta = super()._gce_pv_clip(shown, meta)
        self._sub_gpu_planes = self._sub_gpu_section = None
        if getattr(self, "_sub_draft", None) is not None or getattr(self, "_sub_full", False):
            return shown, meta
        sections = [s for s in self._subsections() if s.get("enabled", True)]
        if not sections:
            return shown, meta
        import mew_subsection
        n = len(shown)
        times, radii = meta.get("ptimes"), meta.get("point_radii")
        have_t = times is not None and len(times) == n
        have_r = radii is not None and len(radii) == n
        gp, gt = meta.get("gcode_paths"), meta.get("gcode_ptimes")
        have_g = bool(gp) and gt is not None and len(gp) == len(gt)
        layers = self._subsection_layers()
        out_p, out_t, out_r, out_gp, out_gt, errors = [], [], [], [], [], []
        planes = true_sec = None
        for section in sections:
            try:
                sec = self._subsection_tube_frame(section)
                if len(sections) == 1:
                    true_sec = sec
                    sec, planes = self._subsection_gpu_cut(sec, meta)
                p, t, r = mew_subsection.section_pieces(
                    sec, shown, times if have_t else None, radii if have_r else None,
                    layers, meta.get("command_ptimes"))
                g = (mew_subsection.section_pieces(sec, gp, gt, None, layers)
                     if have_g else (None, None, None))
            except (ValueError, KeyError, AttributeError, tk.TclError) as exc:
                errors.append("%s: %s" % (section.get("name", "Subsection"), exc))
                continue
            out_p += p
            out_t += t or []
            out_r += r or []
            if have_g:
                out_gp += g[0]
                out_gt += g[1] or []
        if errors and not out_p:
            self.root.after_idle(lambda: self._gce_pv_panel.set_status("; ".join(errors), error=True))
            return shown, meta
        meta2 = dict(meta, ptimes=out_t if have_t else None, command_ptimes=None,
                     n_paths=len(out_p), cap_tubes=True)
        if have_r:
            meta2["point_radii"] = out_r
        if have_g:
            meta2["gcode_paths"], meta2["gcode_ptimes"] = out_gp, out_gt
        self._sub_empty = not out_p
        self._sub_gpu_planes = planes
        self._sub_gpu_section = true_sec if planes is not None else None
        return out_p, meta2

    def _subsection_design_caps(self, panel):
        """The slice's faces in the Design view (the tool closed)."""
        for actor in getattr(self, "_sub_cap_design", ()):
            if panel._ren is not None:
                panel._ren.RemoveActor(actor)
        self._sub_cap_design = []
        sec = getattr(self, "_sub_gpu_section", None)
        planes = getattr(self, "_sub_gpu_planes", None)
        state = panel._state or {}
        if sec is None or planes is None or not state.get("paths") or panel._ren is None:
            return
        meta = state.get("meta") or {}
        try:
            actor = self._subsection_caps(panel, sec, state["paths"],
                                          meta.get("point_radii"), meta)
        except (ValueError, KeyError, AttributeError, RuntimeError):
            return
        if actor is not None:
            self._sub_actors.remove(actor)
            self._sub_cap_design.append(actor)
            panel._render_soon()

    def _jet_start(self):
        # Simulation plays the whole print: the cut is the Design view's
        panel = getattr(self, "_gce_pv_panel", None)
        self._subsection_clear(panel)
        if any(s.get("enabled", True) for s in self._subsections()):
            self._sub_full = True
            self._gce_pv_show()
        super()._jet_start()

    def _jet_stop(self):
        super()._jet_stop()
        if getattr(self, "_sub_full", False):
            self._sub_full = False
            self._gce_pv_show()
        else:
            self._subsection_render()

    def _gce_pv_apply(self, paths, meta, err):
        super()._gce_pv_apply(paths, meta, err)
        if not err and meta and not meta.get("export_quality") \
                and not meta.get("provisional") and (
                getattr(self, "_sub_draft", None) is not None or
                any(s.get("enabled", True) for s in self._subsections())):
            self.root.after_idle(self._subsection_request_exact)

    def _subsection_request_exact(self):
        full = getattr(self, "_gce_pv_full", None)
        if full is None or (full[1] or {}).get("export_quality"):
            return
        if getattr(self, "_gce_eq_proc", None) is None and getattr(self, "proc", None) is None:
            self._gce_export_preview_start()

    def _subsection_unwire(self, panel):
        wire = getattr(self, "_sub_wire", None)
        if wire is not None:
            panel._ren.RemoveActor(wire)
            if wire in getattr(self, "_sub_actors", ()):
                self._sub_actors.remove(wire)
        self._sub_wire = None

    def _subsection_clear(self, panel):
        """Take the tool's drawing off the picture: its actors, the dimming
        of the print under them, the coincident-surface setting they asked for."""
        actors = getattr(self, "_sub_actors", None) or []
        if panel is not None and panel._ren is not None:
            for actor in actors:
                panel._ren.RemoveActor(actor)
            dim = getattr(self, "_sub_dim", None)
            if dim is not None and panel._actor is not None:
                panel._mapper.SetScalarVisibility(dim[0])
                panel._actor.GetProperty().SetColor(*dim[1])
        self._sub_actors, self._sub_pieces, self._sub_wire = [], [], None
        self._sub_drag_actor, self._sub_cut_ready = None, 0.
        self._sub_dim = None
        mode = getattr(self, "_sub_resolve", None)
        if mode is not None:
            import vtk
            vtk.vtkMapper.SetResolveCoincidentTopology(mode)
            self._sub_resolve = None

    def _subsection_render(self):
        """The open tool's picture: the print dimmed, the cut coloured over it,
        the outline and the gimbal. Nothing here is drawn once the tool closes."""
        self._sub_after = None
        panel = getattr(self, "_gce_pv_panel", None)
        draft = getattr(self, "_sub_draft", None)
        if panel is None or panel._ren is None or panel._actor is None:
            return
        if draft is not None and draft.get("drag") is not None:
            self._subsection_drag_view()
            return
        if draft is None or getattr(self, "_jet", None) is not None:
            self._subsection_clear(panel)
            return
        try:
            section = self._subsection_value()
        except (ValueError, KeyError, tk.TclError) as exc:
            self._subsection_clear(panel)
            self._subsection_status(str(exc))
            panel.show_gimbal(None)
            panel._render_soon()
            return
        self._subsection_status("")
        key = json.dumps({k: x for k, x in section.items() if k not in ("name", "id", "enabled")},
                         sort_keys=True, default=float)
        if key == draft.get("rendered") and getattr(self, "_sub_actors", None):
            for actor in self._sub_pieces:
                actor.SetVisibility(1)
            if getattr(self, "_sub_drag_actor", None) is not None:
                self._sub_drag_actor.SetVisibility(0)
            panel.on_gimbal = self._subsection_drag
            panel.show_gimbal(section["center"], ring=True)
            panel._render_soon()
            return
        self._subsection_clear(panel)
        state = panel._state or {}
        paths = state.get("paths") or []
        meta = state.get("meta") or {}
        if not paths:
            return
        import vtk
        import mew_subsection
        # the print dims to grey in place, so it stays the one opaque,
        # clickable picture and the section is drawn over it
        self._sub_dim = (panel._mapper.GetScalarVisibility(), panel._actor.GetProperty().GetColor())
        panel._mapper.ScalarVisibilityOff()
        panel._actor.GetProperty().SetColor(*_DIM)
        try:
            sec = self._subsection_tube_frame(section)
            cut, planes = self._subsection_gpu_cut(sec, meta)
            pieces, times, radii = mew_subsection.section_pieces(
                cut, paths, meta.get("ptimes"), meta.get("point_radii"),
                self._subsection_layers(), meta.get("command_ptimes"))
        except (ValueError, KeyError, AttributeError, tk.TclError) as exc:
            self._subsection_status(str(exc))
            pieces, planes = [], None
        if pieces:
            self._subsection_overlay(panel, state, meta, pieces, times, radii, planes)
            if planes is not None:
                try:
                    self._subsection_caps(panel, sec, pieces, radii, meta, _OVERLAY_GROW)
                except (ValueError, KeyError, AttributeError, RuntimeError):
                    pass
        self._subsection_wireframe(section, panel)
        draft["rendered"] = key
        panel.on_gimbal = self._subsection_drag
        panel.show_gimbal(section["center"], ring=True)
        panel._render_soon()

    def _subsection_overlay(self, panel, state, meta, pieces, times, radii, planes=None):
        """The cut fibres, coloured as the picture colours them, over the dimmed print."""
        import vtk
        meta2 = dict(meta, ptimes=times, command_ptimes=None, n_paths=len(pieces))
        if radii is not None:
            meta2["point_radii"] = [r * _OVERLAY_GROW for r in radii]
        else:
            meta2.pop("point_radii", None)
        tmp = dict(state, paths=pieces, meta=meta2, lines_pd=None, tubes_pd=None,
                   gcode_pd=None, display_mode=None, aux=None, _pick_index=None)
        saved = (getattr(self, "_view_layers", []), getattr(self, "_view_why", None))
        self._gce_pv_aux(tmp, pieces, meta2)
        self._view_layers, self._view_why = saved
        lines = panel._lines_polydata(vtk, tmp)
        source = lines
        import gui_preview
        sides = meta2.get("tube_sides") or gui_preview.PREVIEW_TUBE_SIDES
        if panel._tubes_enabled() and gui_preview.tubes_fit(lines, sides):
            tube = vtk.vtkTubeFilter()
            tube.SetInputData(lines)
            base = max(meta2.get("tube_diameter", meta2["fiber_diameter"]) / 2.0, 1e-5)
            tube.SetRadius(base * _OVERLAY_GROW)
            if lines.GetPointData().HasArray("fiber_radius"):
                tube.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS,
                                            "fiber_radius")
                tube.SetVaryRadiusToVaryRadiusByAbsoluteScalar()
            tube.SetNumberOfSides(sides)
            tube.SetCapping(1)
            tube.Update()
            source = tube.GetOutput()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(source)
        view = getattr(panel, "view_tools", None)
        if view is not None and view.paint_other(mapper, tmp):
            pass
        else:
            mapper.SetScalarVisibility(0)
        mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-3., -3.)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().DeepCopy(panel._actor.GetProperty())
        actor.GetProperty().SetColor(*state.get("color", (0.8, 0.25, 0.2)))
        actor.GetProperty().SetOpacity(1.)
        actor.PickableOff()
        self._subsection_apply_planes(mapper, planes)
        self._sub_resolve = vtk.vtkMapper.GetResolveCoincidentTopology()
        vtk.vtkMapper.SetResolveCoincidentTopologyToPolygonOffset()
        panel._ren.AddActor(actor)
        self._sub_actors.append(actor)
        self._sub_pieces.append(actor)

    def _subsection_wrapped_wire(self, section):
        """The snapped section's outline: its footprint laid on the rod,
        at the top and the bottom of its depth, joined at its corners."""
        import vtk
        import mew_subsection
        _radius, _phi0, flat = mew_subsection.snap_frame(section)
        a = math.radians(float(section.get("rotation", 0)))
        rotation = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        polygon = mew_subsection._outline(section) @ rotation.T
        cx, _cy, rc = flat["center"]
        half = float(section["size"][2]) / 2.0
        n = len(polygon)
        ring = []                      # each edge in steps, so its arc is smooth
        for i in range(n):
            q, r = polygon[i], polygon[(i + 1) % n]
            steps = max(1, int(math.ceil(abs(r[1] - q[1]) / (_radius * math.radians(4)))))
            ring.extend(q + (r - q) * k / steps for k in range(steps))
        ring = np.array(ring)
        pts = vtk.vtkPoints()
        cells = vtk.vtkCellArray()
        corners = []
        for w in (rc - half, rc + half):
            base = pts.GetNumberOfPoints()
            xyz = mew_subsection.roll(
                np.column_stack((cx + ring[:, 0], ring[:, 1], np.full(len(ring), w))), section)
            for x, y, z in xyz:
                pts.InsertNextPoint(float(x), float(y), float(z))
            m = len(ring)
            for k in range(m):
                cells.InsertNextCell(2)
                cells.InsertCellPoint(base + k)
                cells.InsertCellPoint(base + (k + 1) % m)
            corners.append(base)
        m = len(ring)
        for k in range(m):
            if k == 0 or not np.allclose(ring[k] - ring[k - 1], ring[(k + 1) % m] - ring[k]):
                cells.InsertNextCell(2)
                cells.InsertCellPoint(corners[0] + k)
                cells.InsertCellPoint(corners[1] + k)
        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetLines(cells)
        return poly

    def _subsection_wireframe(self, section, panel):
        import vtk
        import mew_subsection
        if section.get("snap_surface"):
            try:
                section = self._subsection_tube_frame(section)
            except (ValueError, KeyError, AttributeError, tk.TclError):
                pass
        if mew_subsection.is_snapped(section):
            poly = self._subsection_wrapped_wire(section)
        elif section["shape"] == "sphere":
            src = vtk.vtkSphereSource()
            src.SetCenter(*section["center"])
            src.SetRadius(section["size"][0] / 2)
            src.SetThetaResolution(32)
            src.SetPhiResolution(24)
            src.Update()
            poly = src.GetOutput()
        else:
            polygon = mew_subsection._outline(section)
            a = math.radians(section["rotation"])
            rotation = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
            polygon = polygon @ rotation.T + np.asarray(section["center"][:2])
            z0 = section["center"][2] - section["size"][2] / 2
            z1 = section["center"][2] + section["size"][2] / 2
            pts = vtk.vtkPoints()
            for z in (z0, z1):
                for x, y in polygon:
                    pts.InsertNextPoint(float(x), float(y), float(z))
            cells = vtk.vtkCellArray()
            n = len(polygon)
            for i in range(n):
                for a, b in ((i, (i + 1) % n), (n + i, n + (i + 1) % n), (i, n + i)):
                    cells.InsertNextCell(2)
                    cells.InsertCellPoint(a)
                    cells.InsertCellPoint(b)
            poly = vtk.vtkPolyData()
            poly.SetPoints(pts)
            poly.SetLines(cells)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetRepresentationToWireframe()
        actor.GetProperty().SetColor(.1, .55, 1.)
        actor.GetProperty().SetLineWidth(2.)
        actor.GetProperty().SetLighting(False)
        actor.PickableOff()
        panel._ren.AddActor(actor)
        self._sub_actors.append(actor)
        self._sub_wire = actor

    def save_result_as(self):
        last = getattr(self, "_gw_result", None) or {}
        from_design = hasattr(self, "gce") and (
            self._gce_on_page() or (last.get("kind") == "page" and
                                    last.get("path") == getattr(self, "last_gcode", None)))
        if not from_design or getattr(self, "_last_gcode_is_calibration", False):
            return super().save_result_as()
        sections = [copy.deepcopy(s) for s in self._subsections() if s.get("enabled", True)]
        if not sections:
            return super().save_result_as()
        import mew_subsection
        for s in sections:
            try:
                mew_subsection.clipping_planes(s)
            except (ValueError, KeyError, TypeError) as exc:
                self._refuse("Not exported: subsection %s cannot be cut: %s"
                             % (s.get("name", ""), exc))
                return
        if self._run_in_flight():
            self._tell_run_in_flight("STL")
            return
        depo = getattr(self, "last_depo", None)
        if not depo or not os.path.isfile(depo):
            self._refuse("Generate the full design before exporting its subsections.")
            return
        stale = self._gw_stale_reason()
        if stale:
            self._refuse(stale["text"] + " Generate it again before export.")
            return
        folder = filedialog.askdirectory(title="Choose a folder for subsection STLs")
        if not folder:
            return
        names = []
        for section in sections:
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", section["name"]).strip(" .")
            if not name:
                self._refuse("Choose a name that can be used as a file name.")
                return
            names.append(name + ".stl")
        if len({x.casefold() for x in names}) != len(names):
            self._refuse("Subsection names must produce different file names.")
            return
        outputs = [os.path.join(folder, name) for name in names]
        existing = [os.path.basename(p) for p in outputs if os.path.exists(p)]
        if existing and not messagebox.askyesno("Replace files?", "Replace these files?\n" + "\n".join(existing)):
            return
        from gui_common import HERE, IS_WINDOWS, WORKER_PIPE_UTF8, worker_cmd
        try:
            if any(s.get("z_mode") == "layers" for s in sections):
                layers = self._subsection_layers()
                if not layers:
                    raise ValueError("The print's layer clock is not ready. Update the preview.")
            for s in sections:
                if s.get("z_mode") == "layers":
                    s["layer_windows"] = [{"t0": row["t0"], "t1": row["t1"]} for row in layers]
        except ValueError as exc:
            self._refuse("Not exported: %s" % exc)
            return
        diam, faces = self._fiber_diam_faces()
        source = self.last_gcode if self.last_gcode and os.path.isfile(self.last_gcode) else depo
        jobs = []
        try:
            for i, (section, out) in enumerate(zip(sections, outputs)):
                section = self._subsection_tube_frame(section)
                # the picture is on the plate or the rod; the cut reads the
                # file, so its centre goes back by the same zero shift (a
                # tube's is along the rod only)
                shift = self._gce_pv_options().get("preview_shift")
                if shift is not None:
                    section["center"] = (np.asarray(section["center"]) - np.asarray(shift)).tolist()
                spec = self._gce_scratch("subsection_%s_%d.json" % (uuid.uuid4().hex, i))
                scratch_out = self._gce_scratch("subsection_%s_%d.stl" % (uuid.uuid4().hex, i))
                with open(spec, "w", encoding="utf-8") as fh:
                    json.dump(section, fh)
                args = worker_cmd("worker:convert", source, "--from-depo", depo,
                                  "--subsection", spec, "-d", repr(diam), "-f", str(faces),
                                  "--mesh-tolerance", repr(self._fiber_mesh_tol()), "-o", scratch_out)
                tis = getattr(self, "s_travel_in_stl", None)
                if tis is not None and tis.get():
                    args.append("--travel-in-stl")
                jobs.append((args, scratch_out, out))
        except (OSError, ValueError, AttributeError) as exc:
            self._refuse("Not exported: %s" % exc)
            return
        self._exact_build_busy = True
        self.cancelled = False
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        result = {}
        def work():
            try:
                for args, scratch_out, _out in jobs:
                    proc = subprocess.Popen(args, cwd=HERE, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT,
                                            creationflags=0x08000000 if IS_WINDOWS else 0,
                                            **WORKER_PIPE_UTF8)
                    self.proc = proc
                    log, _ = proc.communicate()
                    if proc.returncode or not os.path.isfile(scratch_out):
                        result["error"] = log[-1200:] if log else "The worker failed."
                        break
                    if self.cancelled:
                        break
                if not self.cancelled and not result.get("error"):
                    for _args, scratch_out, out in jobs:
                        shutil.copy2(scratch_out, out)
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.status.set("Building subsection STLs…")
        def poll():
            if thread.is_alive():
                self.root.after(100, poll)
                return
            self.proc = None
            self._exact_build_busy = False
            self.run_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            if self.cancelled:
                self.status.set("Subsection export cancelled.")
            elif result.get("error"):
                self.status.set("Subsection export failed.")
                self._refuse("Not exported: %s" % result["error"])
            else:
                self.status.set("Saved %d subsection STL%s." % (len(outputs), "s" if len(outputs) != 1 else ""))
        self.root.after(100, poll)
