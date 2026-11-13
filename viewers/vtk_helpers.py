#!/usr/bin/env python3
"""
vtk_helpers.py

Small VTK actor helpers shared by the two standalone viewers -- stl_viewer.py
and gcode_player.py -- which the GUI launches as separate processes. Keeping the
shared bits here means the origin marker is defined once and stays identical in
both windows.

Requires: vtk (already a dependency of this project's environment).
"""

import math
import os
import sys

import vtk


def free_memory_bytes():
    """The memory a mesh about to be built can take, in bytes, or None where it
    cannot be read. The Design preview and the Simulation viewer size a mesh
    against it before building one, because VTK aborts the whole process when an
    allocation fails: there is no exception to catch.

    On Windows this IS `memory_headroom_bytes`, so a job's memory cap counts:
    free physical memory alone reads the whole machine and never falls inside a
    capped job, so a step-down sized on it never fired there. Elsewhere it is
    the physical memory free (sysconf), as before."""
    if sys.platform == "win32":
        return memory_headroom_bytes()
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None


def memory_headroom_bytes():
    """The memory this process can still take before it would fail, in bytes:
    the physical memory free to use now (GlobalMemoryStatusEx ullAvailPhys),
    or less when a Windows job caps this process (a job memory limit less
    what the job has committed, or a per-process limit less this process's
    commit). The jobs read are this process's own and the one named by
    MEW_JOB_NAME: the venv's python.exe launcher nests the interpreter in a
    job of its own, so a cap set on the launcher's job (blind_rig/hidden.py)
    is invisible to the NULL query. None when it cannot be read (not Windows, or a ctypes failure):
    a budget built on it then draws in full, as it would on a machine with
    room. Read on the thread that sizes a budget, at the moment it is sized.

    Every memory budget reads this one function, so a machine with room draws
    full detail and only one that would really fail steps down."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes as wt

        class _Status(ctypes.Structure):
            _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD)] + [
                (n, ctypes.c_ulonglong) for n in (
                    "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                    "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual",
                    "ullAvailExtendedVirtual")]

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class _Extended(ctypes.Structure):
            _fields_ = [("Basic", _Basic),
                        ("Io", ctypes.c_ulonglong * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class _JobUse(ctypes.Structure):     # JOBOBJECT_MEMORY_USAGE_INFORMATION
            _fields_ = [("JobMemory", ctypes.c_ulonglong),
                        ("PeakJobMemoryUsed", ctypes.c_ulonglong)]

        class _Counters(ctypes.Structure):   # PROCESS_MEMORY_COUNTERS
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
                (n, ctypes.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        st = _Status()
        st.dwLength = ctypes.sizeof(st)
        if not k32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        room = int(st.ullAvailPhys)

        query = k32.QueryInformationJobObject
        query.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                          wt.DWORD, ctypes.c_void_p]
        query.restype = wt.BOOL
        jobs = [None]                         # this process's own job
        named = os.environ.get("MEW_JOB_NAME")
        if named:
            k32.OpenJobObjectW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
            k32.OpenJobObjectW.restype = wt.HANDLE
            h = k32.OpenJobObjectW(0x0004, False, named)  # JOB_OBJECT_QUERY
            if h:
                jobs.append(h)
        flags, plimit = 0, 0
        for job in jobs:
            ext = _Extended()
            if not query(job, 9, ctypes.byref(ext), ctypes.sizeof(ext),
                         None):
                continue                      # in no job: physical free
            jf = ext.Basic.LimitFlags
            flags |= jf
            if jf & 0x200 and ext.JobMemoryLimit:        # LIMIT_JOB_MEMORY
                use = _JobUse()
                if query(job, 28, ctypes.byref(use), ctypes.sizeof(use),
                         None):
                    room = min(room, int(ext.JobMemoryLimit)
                               - int(use.JobMemory))
            if jf & 0x100 and ext.ProcessMemoryLimit:
                plimit = int(ext.ProcessMemoryLimit)
        for job in jobs[1:]:
            k32.CloseHandle(wt.HANDLE(job))
        if flags & 0x100 and plimit:                     # LIMIT_PROCESS_MEMORY
            get = k32.K32GetProcessMemoryInfo
            get.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
            get.restype = wt.BOOL
            k32.GetCurrentProcess.restype = wt.HANDLE
            pmc = _Counters()
            pmc.cb = ctypes.sizeof(pmc)
            if get(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                room = min(room, plimit - int(pmc.PagefileUsage))
        return max(0, room)
    except (OSError, AttributeError, ValueError):
        return None


def origin_marker(length):
    """A small RGB axis triad at (0,0,0) -- X red, Y green, Z blue -- marking the
    machine origin. Three unlit lines, so it stays cheap and always legible."""
    pts = vtk.vtkPoints()
    pts.InsertNextPoint(0.0, 0.0, 0.0)
    pts.InsertNextPoint(length, 0.0, 0.0)
    pts.InsertNextPoint(0.0, length, 0.0)
    pts.InsertNextPoint(0.0, 0.0, length)
    lines = vtk.vtkCellArray()
    for a, b in ((0, 1), (0, 2), (0, 3)):
        lines.InsertNextCell(2)
        lines.InsertCellPoint(a)
        lines.InsertCellPoint(b)
    colors = vtk.vtkUnsignedCharArray()
    colors.SetNumberOfComponents(3)
    colors.InsertNextTuple3(235, 70, 70)     # +X red
    colors.InsertNextTuple3(70, 210, 70)     # +Y green
    colors.InsertNextTuple3(90, 130, 255)    # +Z blue
    poly = vtk.vtkPolyData()
    poly.SetPoints(pts)
    poly.SetLines(lines)
    poly.GetCellData().SetScalars(colors)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(poly)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetLineWidth(2.0)
    actor.GetProperty().SetLighting(False)
    return actor


# Studio lighting. VTK's default is ONE headlight, created when a renderer first
# renders with an empty light list. A headlight sits AT the camera, so N.L is ~1
# across every surface facing you -- on a cylinder that flattens the whole
# cross-section to one value, and a fibre renders as a flat pale bar however many
# facets the tube has. It is also under one screen pixel wide on a 20 mm scaffold
# (0.90 px in a 900 px viewport), so multisampling compounds it.
# A key/fill/back kit puts the light OFF-axis, and the gradient across a tube is
# what makes it read as round. The numbers below survived an A/B against the
# headlight on a 15x15 fibre lattice; they are gentle
# on purpose, a scaffold being a near-white object a hot key would blow out.
_KEY_TO_FILL = 2.6        # key/fill contrast -- the shaded side stays readable
_KEY_TO_HEAD = 3.5        # a little head light so silhouettes don't go black
_KEY_TO_BACK = 3.0        # rim separation between stacked layers
_KEY_WARMTH = 0.58        # slightly warm key, cool fill: the standard CAD pairing
_FILL_WARMTH = 0.42


def studio_light_kit():
    """A configured vtkLightKit (key / fill / back / head) for a viewer's scene.

    The caller OWNS the kit -- VTK renderers do not accept arbitrary Python
    attributes, so it cannot be stashed on the renderer. Hold it and call
    AddLightsToRenderer / RemoveLightsFromRenderer to switch it in and out;
    removing every light is safe, because vtkRenderer re-creates its automatic
    headlight on the next Render when the list is empty. That is what makes the
    toggle a true A/B rather than a one-way trip."""
    kit = vtk.vtkLightKit()
    kit.SetKeyLightIntensity(1.0)
    kit.SetKeyToFillRatio(_KEY_TO_FILL)
    kit.SetKeyToHeadRatio(_KEY_TO_HEAD)
    kit.SetKeyToBackRatio(_KEY_TO_BACK)
    kit.SetKeyLightWarmth(_KEY_WARMTH)
    kit.SetFillLightWarmth(_FILL_WARMTH)
    return kit


# The deposition mesh's material. VTK's vtkProperty defaults are purely diffuse
# with no highlight, which the deposition actors were always left at; under the
# studio kit the highlight has an off-axis light to come from. `False` restores
# the VTK defaults exactly, so toggling back is byte-identical to the old look.
# specular 0.18 / power 18, NOT the 0.45 / 30 this first shipped with. Measured
# on real sagged geometry zoomed to a crossover (8 layers, 3 mm, 264k tris): at
# 0.45 the crest CLIPS -- peak 255, 1815 pixels at or above 240 -- which is the
# white blob at junctions; 0.28 -> 1588 px, 0.18 -> 936 and no clipping. Mean
# fibre luminance is 175 at every setting, so backing off costs no brightness.
# The measurement must run on SAGGED geometry: the first attempt used idealised
# non-touching cylinders, whose lobe never reached the camera, and returned an
# identical peak for all four settings.
_STUDIO_MATERIAL = dict(ambient=0.15, diffuse=0.75, specular=0.18, power=18.0)
_FLAT_MATERIAL = dict(ambient=0.0, diffuse=1.0, specular=0.0, power=1.0)


def set_fibre_material(prop, studio):
    """Apply the studio (or the stock VTK) material to one vtkProperty.

    Only the light-response terms are touched -- colour, opacity and scalar
    visibility are left alone, so this composes with per-scaffold colouring and
    the deviation LUT rather than fighting them."""
    m = _STUDIO_MATERIAL if studio else _FLAT_MATERIAL
    prop.SetAmbient(m["ambient"])
    prop.SetDiffuse(m["diffuse"])
    prop.SetSpecular(m["specular"])
    prop.SetSpecularPower(m["power"])


def fit_clipping_range(renderer, margin=1.5):
    """Reset the camera's near/far clipping planes so the WHOLE visible scene stays
    between them for any zoom or orbit -- the robust fix for parts of a large model
    getting sliced away as you orbit (the near plane "guillotining" the geometry that
    swings behind the camera).

    Why orbiting clips at all: VTK clamps the near plane to a small POSITIVE fraction
    of the far plane, so anything that falls behind the camera POINT is dropped. Under
    parallel (orthographic) projection the camera can sit *inside* the scene's depth --
    e.g. when the view was framed on a small detail of a big machine, the camera ends
    up near the model centre -- so as you orbit, whichever half is momentarily behind
    the camera vanishes (you "see only part of the machine at a time").

    The cure exploits a property unique to parallel projection: sliding the camera
    along its own view direction does not change the image at all (zoom there is the
    parallel SCALE, not the camera distance). So we push the camera straight back until
    the scene's bounding SPHERE is entirely in front of it, then reset the range. The
    picture is untouched, but every point now lies safely between near and far -- and
    because a sphere is rotation-invariant, the guarantee holds whichever way the user
    then orbits, not just the current angle.

    Perspective projection, an empty scene, or a camera already far enough back are all
    left to the plain reset (we only ever push the camera OUT, never pull it in, so this
    never fights the user's zoom or drifts the view). `margin` is the camera->centre
    distance wanted as a multiple of the bounding-sphere radius: >=1 clears the sphere,
    1.5 leaves comfortable head-room so small in-scene motion never re-clips."""
    cam = renderer.GetActiveCamera()
    b = renderer.ComputeVisiblePropBounds()
    # empty / invalid scene (inverted bounds) -> nothing to fit; the stock reset
    # already no-ops safely on such bounds.
    if b[0] > b[1] or b[2] > b[3] or b[4] > b[5]:
        renderer.ResetCameraClippingRange()
        return
    if cam.GetParallelProjection():
        cx, cy, cz = (b[0] + b[1]) * 0.5, (b[2] + b[3]) * 0.5, (b[4] + b[5]) * 0.5
        radius = 0.5 * math.sqrt((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2
                                 + (b[5] - b[4]) ** 2)
        px, py, pz = cam.GetPosition()
        fx, fy, fz = cam.GetFocalPoint()
        vx, vy, vz = fx - px, fy - py, fz - pz      # view direction (camera -> focus)
        vn = math.sqrt(vx * vx + vy * vy + vz * vz)
        if radius > 0.0 and vn > 1e-12:
            vx, vy, vz = vx / vn, vy / vn, vz / vn
            # signed camera->centre distance ALONG the view axis; < need means the
            # sphere pokes behind the camera, so slide the camera back by the deficit.
            along = (cx - px) * vx + (cy - py) * vy + (cz - pz) * vz
            need = radius * margin
            if along < need:
                push = need - along
                cam.SetPosition(px - vx * push, py - vy * push, pz - vz * push)
    # Reset the range from the bounds ALREADY computed above, not the no-arg
    # ResetCameraClippingRange() -- that would rescan every actor's bounds a SECOND
    # time (ComputeVisiblePropBounds). This runs on every orbit/zoom frame, so over a
    # scaffold split into many deposition chunk-actors the duplicate scan was pure
    # per-frame waste; the explicit-bounds overload gives the identical range.
    renderer.ResetCameraClippingRange(b[0], b[1], b[2], b[3], b[4], b[5])


def set_projection(renderer, parallel):
    """Flip the camera between orthographic (`parallel=True`) and perspective
    WITHOUT changing how big the scene looks, then re-fit the clipping range.

    The two projections express "zoom" with different quantities, so a bare
    `SetParallelProjection` jumps the image. Orthographic zoom is the parallel
    SCALE: the half-height, in world mm, of the slab the viewport shows, at every
    depth. Perspective zoom is the camera DISTANCE: at distance d and view angle
    th, the half-height of what fills the viewport AT THE FOCAL POINT is
    d*tan(th/2). Equating the two half-heights is the whole conversion --

        ortho -> perspective:  d = scale / tan(th/2)   (slide along the view axis)
        perspective -> ortho:  scale = d * tan(th/2)   (position unchanged)

    -- so the focal-point plane keeps its on-screen size across the switch and
    only the depth cue (converging vs parallel edges) changes, which is the one
    thing the toggle is for. The view DIRECTION and view-up are untouched, so an
    orbit in progress and the ViewCube both stay in step.

    Perspective costs the unlimited zoom-in: install_cad_camera drives parallel
    zoom by shrinking the parallel scale, which has no floor, whereas perspective
    falls back to cam.Dolly() and stops against the near-clip clamp. That is the
    documented reason the viewers default to orthographic (stl_viewer's camera
    setup), and it is why this is a toggle rather than a change of default."""
    cam = renderer.GetActiveCamera()
    parallel = bool(parallel)
    if bool(cam.GetParallelProjection()) == parallel:
        return False                       # already there; never disturb the camera
    half_angle = math.radians(cam.GetViewAngle()) * 0.5
    t = math.tan(half_angle)
    fx, fy, fz = cam.GetFocalPoint()
    px, py, pz = cam.GetPosition()
    vx, vy, vz = px - fx, py - fy, pz - fz  # focus -> camera
    d = math.sqrt(vx * vx + vy * vy + vz * vz)
    if parallel:
        # perspective -> ortho: the slab half-height IS what the frustum already
        # shows at the focal plane, so the camera does not move at all.
        if d > 0.0 and t > 0.0:
            cam.SetParallelScale(max(d * t, 1e-9))
        cam.ParallelProjectionOn()
    else:
        # ortho -> perspective: slide the camera to the distance whose frustum is
        # that same half-height at the focal point. Degenerate inputs (a zero
        # scale, a camera sitting on its own focal point) leave the distance
        # alone rather than dividing by zero.
        scale = cam.GetParallelScale()
        if t > 0.0 and scale > 0.0 and d > 1e-12:
            want = scale / t
            k = want / d
            cam.SetPosition(fx + vx * k, fy + vy * k, fz + vz * k)
        cam.ParallelProjectionOff()
    fit_clipping_range(renderer)
    return True


def projection_toggle(renderer):
    """`(label_fn, callback, state_fn)` for a viewer sidebar row that switches the
    camera projection. The three read the CAMERA, never a cached flag, so the row
    cannot drift out of step with a camera something else reset.

    `state_fn()` is true while PERSPECTIVE is on, matching the switch name the
    rail draws ("Perspective"), so the switch is lit exactly when the non-default
    projection is active."""
    def _perspective_on():
        return not bool(renderer.GetActiveCamera().GetParallelProjection())

    def _label():
        return "Orthographic view" if _perspective_on() else "Perspective view"

    def _callback():
        set_projection(renderer, parallel=_perspective_on())

    return _label, _callback, _perspective_on


#: the Settings window's "Perspective view" (gui_prefs `perspective`), read
#: once per process; the App's switch updates it with `set_prefer_perspective`
_PERSPECTIVE = None


def prefer_perspective():
    """True when the user asked every viewer to open in perspective."""
    global _PERSPECTIVE
    if _PERSPECTIVE is None:
        try:
            import gui_prefs
            _PERSPECTIVE = bool(gui_prefs.get("perspective", False))
        except Exception:  # noqa: BLE001 -- no prefs: the orthographic default
            _PERSPECTIVE = False
    return _PERSPECTIVE


def set_prefer_perspective(on):
    global _PERSPECTIVE
    _PERSPECTIVE = bool(on)


#: the Settings window's "Show G-code zero" (gui_prefs `show_origin`, default
#: on): the XYZ triad at g-code zero in the design preview and the viewers
_SHOW_ORIGIN = None


def prefer_origin():
    global _SHOW_ORIGIN
    if _SHOW_ORIGIN is None:
        try:
            import gui_prefs
            _SHOW_ORIGIN = bool(gui_prefs.get("show_origin", True))
        except Exception:  # noqa: BLE001 -- no prefs: shown, the default
            _SHOW_ORIGIN = True
    return _SHOW_ORIGIN


def set_prefer_origin(on):
    global _SHOW_ORIGIN
    _SHOW_ORIGIN = bool(on)


def apply_preferred_projection(renderer):
    """After a viewer frames its opening view (always in orthographic, where
    the framing maths lives): switch to perspective at the same apparent size
    when the Settings ask for it."""
    if prefer_perspective():
        set_projection(renderer, parallel=False)


def install_cad_camera(interactor, renderer, zoom=1.15):
    """Make a trackball interactor orbit and zoom about the point under the cursor
    (CAD-style) instead of the fixed model centre, so inspecting a detail keeps it
    in view rather than flinging the whole part across the screen.

    Left-drag orbits freely about the cursor point; RIGHT-drag is a turntable that
    spins purely about the world Z (vertical) axis through the cursor point, so you
    can swing a full 180 deg around the part without the free orbit also tipping it
    (left-drag rotates about all axes at once, which makes a clean half-turn fiddly).
    The wheel zooms toward the cursor. Shift/ctrl+left (pan/spin) and middle-drag
    (pan) stay with the trackball. Call after SetInteractorStyle(trackball), once
    the renderer is in the window.

    Hung on the interactor as observers because VTK ignores Python overrides of
    style methods. Priority 0.3 sits below VTK widgets (0.5) and the viewers'
    button observers (10) -- so sliders and buttons keep their clicks -- but above
    the base trackball (0), whose left-drag rotate AND right-drag dolly it pre-empts
    via the abort flag (the wheel already covers zoom, so right-drag is free to reuse).
    """
    NAV = 0.3
    state = {"orbiting": False, "zturning": False,
             "pivot": (0.0, 0.0, 0.0), "last": (0, 0)}

    def _world(x, y, z):                       # display pixel + depth -> world point
        renderer.SetDisplayPoint(x, y, z)
        renderer.DisplayToWorld()
        w = renderer.GetWorldPoint()
        return (w[0] / w[3], w[1] / w[3], w[2] / w[3]) if w[3] else w[:3]

    def _focal_plane(x, y):                    # cursor point at the focal-point depth
        fx, fy, fz = renderer.GetActiveCamera().GetFocalPoint()
        renderer.SetWorldPoint(fx, fy, fz, 1.0)
        renderer.WorldToDisplay()
        return _world(x, y, renderer.GetDisplayPoint()[2])

    def _depth(x, y):
        """The depth buffer under the cursor, read at the WINDOW level.

        Deliberately NOT renderer.GetZ, which is the obvious call and is broken:
        VTK 9.6 regressed it for a renderer whose viewport is INSET. It bounds-
        checks the pixel against the viewport's SIZE instead of its extent within
        the window. The themed viewers put the scene right of the 240-px sidebar,
        so the scene spans x=[240, W] while GetZ only answers for x <= W-240 --
        blinding exactly the RIGHTMOST SIDEBAR-WIDTH STRIP of the view, where it
        returns a bogus 1.0. _pivot's `0 < z < 1` guard reads that as "no model
        here" and silently drops the orbit back to the focal plane. (That strip
        is a fixed 240 px, so it is the whole right 60% of a 640-px test window
        but ~24% of the player's 1240-px default -- measured 18-23 px of pivot
        slide there without this, 0.00 px with it.)

        Measured: on 9.6 the depth buffer itself is perfect and identical to 9.3
        (window.GetZbufferData spans the test sphere at x=[239..554] on both, and
        the colour buffer matches too) -- only renderer.GetZ truncates, to
        x=[239..400]. So the pixels were never lost; the reader was. This is NOT
        U9's PreserveDepthBufferOn regressing: that still works on 9.6 (turn it
        off and the depth really does vanish, on both versions).

        window.GetZbufferDataAtPoint returns the same correct value on 9.3 and
        9.6 alike, so this is a straight fix, not a version shim. Falls back to
        the old call if a build lacks the window query."""
        win = renderer.GetRenderWindow() or interactor.GetRenderWindow()
        if win is not None:
            try:
                return win.GetZbufferDataAtPoint(int(x), int(y))
            except (AttributeError, TypeError):
                pass
        return renderer.GetZ(int(x), int(y))

    def _pivot(x, y):                          # the model point under the cursor...
        z = _depth(x, y)
        return _world(x, y, z) if 0.0 < z < 1.0 else _focal_plane(x, y)  # ...or fallback

    def _rotate(axis, angle, p):               # rigid camera rotation about p
        cam = renderer.GetActiveCamera()
        t = vtk.vtkTransform()
        t.Translate(p[0], p[1], p[2])
        t.RotateWXYZ(angle, axis[0], axis[1], axis[2])
        t.Translate(-p[0], -p[1], -p[2])
        cam.SetPosition(t.TransformPoint(cam.GetPosition()))
        cam.SetFocalPoint(t.TransformPoint(cam.GetFocalPoint()))
        r = vtk.vtkTransform()
        r.RotateWXYZ(angle, axis[0], axis[1], axis[2])
        cam.SetViewUp(r.TransformVector(cam.GetViewUp()))

    def _commit():
        fit_clipping_range(renderer)            # keep the whole scene un-clipped on orbit/zoom
        if interactor.GetLightFollowCamera():
            renderer.UpdateLightsGeometryToFollowCamera()
        interactor.Render()

    def _orbit():
        x, y = interactor.GetEventPosition()
        lx, ly = state["last"]
        state["last"] = (x, y)
        w, h = renderer.GetSize()
        if not (w and h):
            return
        cam = renderer.GetActiveCamera()
        p = state["pivot"]
        # the trackball's exact azimuth (about view-up) then elevation (about the
        # screen-horizontal axis), only pivoting about p instead of the focal point
        _rotate(cam.GetViewUp(), -200.0 * (x - lx) / w, p)
        pos, fp, up = cam.GetPosition(), cam.GetFocalPoint(), cam.GetViewUp()
        nvd = (pos[0] - fp[0], pos[1] - fp[1], pos[2] - fp[2])
        right = (nvd[1] * up[2] - nvd[2] * up[1],
                 nvd[2] * up[0] - nvd[0] * up[2],
                 nvd[0] * up[1] - nvd[1] * up[0])
        _rotate(right, -200.0 * (y - ly) / h, p)
        cam.OrthogonalizeViewUp()
        interactor.InvokeEvent(vtk.vtkCommand.InteractionEvent)  # keep player clock alive
        _commit()

    def _zturn():
        # turntable: horizontal drag rotates the camera about the WORLD Z axis
        # through the pivot (vertical drag is ignored, so it is a clean spin, not
        # a free orbit). Z is up in these scenes, so this is the "walk around the
        # part" motion, and a full 180 is one easy horizontal sweep.
        x, y = interactor.GetEventPosition()
        lx, _ly = state["last"]
        state["last"] = (x, y)
        w, h = renderer.GetSize()
        if not (w and h):
            return
        # NB: no OrthogonalizeViewUp here -- rotating the view-up about world Z
        # keeps it (0,0,1) so the horizon stays level (a true turntable);
        # orthogonalizing would tip it off vertical and skew the spin.
        _rotate((0.0, 0.0, 1.0), -200.0 * (x - lx) / w, state["pivot"])
        interactor.InvokeEvent(vtk.vtkCommand.InteractionEvent)  # keep player clock alive
        _commit()

    def _zoom(factor):
        cam = renderer.GetActiveCamera()
        x, y = interactor.GetEventPosition()
        before = _focal_plane(x, y)
        if cam.GetParallelProjection():
            cam.SetParallelScale(cam.GetParallelScale() / factor)
        else:
            cam.Dolly(factor)
        after = _focal_plane(x, y)             # shift so the cursor point stays put
        fp, pos = cam.GetFocalPoint(), cam.GetPosition()
        s = (before[0] - after[0], before[1] - after[1], before[2] - after[2])
        cam.SetFocalPoint(fp[0] + s[0], fp[1] + s[1], fp[2] + s[2])
        cam.SetPosition(pos[0] + s[0], pos[1] + s[1], pos[2] + s[2])
        _commit()

    def _press():
        if interactor.GetShiftKey() or interactor.GetControlKey():
            return False                       # leave pan/spin to the trackball
        x, y = interactor.GetEventPosition()
        state.update(pivot=_pivot(x, y), last=(x, y), orbiting=True)
        return True

    def _move():
        if state["orbiting"]:
            _orbit()
            return True
        if state["zturning"]:
            _zturn()
            return True
        return False

    def _release():
        if not state["orbiting"]:
            return False
        state["orbiting"] = False
        return True

    def _press_z():                            # right-drag: world-Z turntable
        if interactor.GetShiftKey() or interactor.GetControlKey():
            return False
        x, y = interactor.GetEventPosition()
        state.update(pivot=_pivot(x, y), last=(x, y), zturning=True)
        return True

    def _release_z():
        if not state["zturning"]:
            return False
        state["zturning"] = False
        return True

    # bind each handler so that, when it acts (returns anything but False), it
    # aborts the event -- pre-empting the trackball's own handler for that event
    def _bind(event, handler):
        def cb(obj, _evt):
            if handler() is not False:
                obj.GetCommand(tag).SetAbortFlag(1)
        tag = interactor.AddObserver(event, cb, NAV)

    _bind("LeftButtonPressEvent", _press)
    _bind("RightButtonPressEvent", _press_z)
    _bind("MouseMoveEvent", _move)
    _bind("LeftButtonReleaseEvent", _release)
    _bind("RightButtonReleaseEvent", _release_z)
    _bind("MouseWheelForwardEvent", lambda: _zoom(zoom))
    _bind("MouseWheelBackwardEvent", lambda: _zoom(1.0 / zoom))
