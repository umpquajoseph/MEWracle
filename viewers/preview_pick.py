"""preview_pick.py -- click-to-select in the embedded scaffold preview. No Tk.

The preview (`gui_preview.PreviewPanel`) is an OFFSCREEN VTK render blitted
onto a Tk label, so there is no VTK interactor and no picker wired to the
screen. A click is resolved here in three steps, each a pure function:

  1. PIXEL -> FIBER (`pick_segment`). Every drawn centreline segment is
     projected through the camera's composite matrix (the same matrix VTK
     renders with, so a projected point lands on the pixel that drew it), and
     the segments passing within `radius_px` of the click are the candidates.
     A candidate counts only if it is VISIBLE: its depth at the closest point
     must not lie behind the render's own depth buffer at that pixel by more
     than `depth_eps` (about a fiber radius plus slack, since a tube's front
     surface sits one radius in front of its centreline). So a click on the top
     layer of a grid picks the top layer even though every layer below projects
     to the same pixel, and a click in a pore picks the fiber seen through it.
     The nearest visible candidate wins; within a pixel of it, the one nearer
     the camera. Segments, not points: a straight pass is two points.
  2. FIBER -> LINE (`time_at`, `line_at_time`). The picked point's print time
     (the drawn paths' own `ptimes`, interpolated along the segment) is looked
     up in the player's line clock (`gcode_player.parse_path`'s `times` +
     `seg_line`) -- the clock the G-code page's caret highlight already uses in
     the other direction, line -> fiber.
  3. LINE -> LAYER (`layer_at_line`). The document's outline
     (`mew_gcode.Document.outline`) gives every scaffold/layer its first and
     last export line; the line falls in exactly one.

`layer_polylines` goes back the other way for the highlight: a layer's line
range -> its print-time window -> the drawn fiber laid inside it, cut at the
window's ends by interpolation, so a continuous fiber that runs on into the
next layer is lit only for this one.
"""

import numpy as np


def segment_index(paths):
    """Concatenated points of `paths` and the start index of every segment
    that joins two points of ONE path. Cache it per picture: it is what every
    click reuses (`gui_preview` keeps it on the state dict)."""
    P = np.ascontiguousarray(np.concatenate(
        [np.asarray(p, np.float64) for p in paths], axis=0))
    counts = np.array([len(p) for p in paths], dtype=np.int64)
    starts = np.zeros(len(paths) + 1, dtype=np.int64)
    np.cumsum(counts, out=starts[1:])
    ok = np.ones(len(P), dtype=bool)
    ok[starts[1:] - 1] = False          # a path's last point starts nothing
    seg = np.flatnonzero(ok[:-1]) if len(P) > 1 else np.zeros(0, np.int64)
    return dict(P=P, starts=starts, seg=seg)


def project(P, M, w, h):
    """World points -> (x, y, depth): x, y in DISPLAY pixels (VTK's, y up),
    depth in the z-buffer's [0, 1]. `M` is the 4x4 from
    `camera.GetCompositeProjectionTransformMatrix(aspect, 0, 1)`, the matrix
    `vtkRenderer.WorldToView` uses; `ViewToDisplay` then maps [-1, 1] onto the
    window."""
    M = np.asarray(M, np.float64)
    q = P @ M[:3, :3].T + M[:3, 3]
    wq = P @ M[3, :3] + M[3, 3]
    wq = np.where(np.abs(wq) < 1e-300, 1e-300, wq)
    x = (q[:, 0] / wq + 1.0) * 0.5 * w
    y = (q[:, 1] / wq + 1.0) * 0.5 * h
    return x, y, q[:, 2] / wq


def pick_segment(index, M, w, h, x, y, zread, radius_px=6.0, depth_eps=0.0,
                 surface_tol=None):
    """The drawn segment seen at display pixel (x, y), or None.

    `zread(x0, y0, x1, y1)` returns the depth buffer over that inclusive
    display-pixel box as an array [y - y0, x - x0] (VTK's rows, bottom up).
    `surface_tol` (world units) is how far a drawn surface lies from its
    centreline -- a tube's corner radius; `depth_eps` the same in buffer
    units, for the visibility test of the fallback.
    Returns dict(path, seg, t, point, dist_px): `path` the index into the
    drawn paths, `seg` the segment's first point within that path, `t` in
    [0, 1] along it, `point` the world point there."""
    P, seg, starts = index["P"], index["seg"], index["starts"]
    if len(seg) == 0:
        return None
    sx, sy, sz = project(P, M, w, h)
    ax, ay, bx, by = sx[seg], sy[seg], sx[seg + 1], sy[seg + 1]
    r = float(radius_px)
    near = ((np.minimum(ax, bx) <= x + r) & (np.maximum(ax, bx) >= x - r)
            & (np.minimum(ay, by) <= y + r) & (np.maximum(ay, by) >= y - r))
    k = seg[near]
    if len(k) == 0:
        return None
    ax, ay, bx, by = sx[k], sy[k], sx[k + 1], sy[k + 1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = np.where(L2 > 0, ((x - ax) * dx + (y - ay) * dy)
                 / np.where(L2 > 0, L2, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    cx, cy = ax + t * dx, ay + t * dy
    dist = np.hypot(cx - x, cy - y)
    keep = dist <= r
    if not np.any(keep):
        return None
    k, t, cx, cy, dist = k[keep], t[keep], cx[keep], cy[keep], dist[keep]
    depth = sz[k] + t * (sz[k + 1] - sz[k])
    # the depth buffer over the click's box: every candidate's pixel is in it
    ri = int(np.ceil(r)) + 1
    x0, y0 = max(int(round(x)) - ri, 0), max(int(round(y)) - ri, 0)
    x1, y1 = min(int(round(x)) + ri, int(w) - 1), min(int(round(y)) + ri,
                                                      int(h) - 1)
    if x1 < x0 or y1 < y0:
        return None
    zb = np.asarray(zread(x0, y0, x1, y1), np.float64).reshape(
        y1 - y0 + 1, x1 - x0 + 1)
    px = np.clip(np.rint(cx).astype(np.int64), x0, x1) - x0
    py = np.clip(np.rint(cy).astype(np.int64), y0, y1) - y0
    # First choice: the fiber DRAWN at the clicked pixel itself. The buffer
    # there, unprojected, is the surface the user sees; the fiber whose tube
    # it lies on (within `surface_tol` of the centreline, in WORLD units) is
    # the one clicked. Without this, a click on the very edge of a tube where
    # a fiber below shows past the silhouette went to the one on top, and at
    # a grazing view a depth comparison across the few pixels from click to
    # centreline rejected the right fiber outright.
    zc = zb[min(max(int(round(y)) - y0, 0), y1 - y0),
            min(max(int(round(x)) - x0, 0), x1 - x0)]
    if zc < 1.0 and surface_tol:
        W = unproject(M, w, h, x, y, zc)
        A, Bv = P[k], P[k + 1]
        ab = Bv - A
        s = np.clip(np.einsum("ij,ij->i", W - A, ab)
                    / np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-300),
                    0.0, 1.0)
        d3 = np.linalg.norm(A + s[:, None] * ab - W, axis=1)
        on = d3 <= surface_tol
        if np.any(on):
            j = int(np.argmin(np.where(on, d3, np.inf)))
            return _hit(P, starts, int(k[j]), float(s[j]), float(dist[j]))
    # Else (a click beside a 1 px line, on the plate, on an overlay): the
    # screen-nearest candidate visible at its own nearest pixel; within a
    # pixel of it, the one nearer the camera.
    keep = depth <= zb[py, px] + depth_eps
    if not np.any(keep):
        return None
    k, t, dist, depth = k[keep], t[keep], dist[keep], depth[keep]
    close = dist <= dist.min() + 1.0
    j = np.flatnonzero(close)[np.argmin(depth[close])]
    return _hit(P, starts, int(k[j]), float(t[j]), float(dist[j]))


def unproject(M, w, h, x, y, z):
    """Display pixel (x, y) at buffer depth z -> the world point: the
    inverse of `project`."""
    v = np.array([2.0 * x / w - 1.0, 2.0 * y / h - 1.0, z, 1.0])
    q = np.linalg.solve(np.asarray(M, np.float64), v)
    return q[:3] / q[3]


def _hit(P, starts, g, tj, dist_px):
    path = int(np.searchsorted(starts, g, side="right") - 1)
    return dict(path=path, seg=int(g - starts[path]), t=tj,
                point=P[g] + tj * (P[g + 1] - P[g]), dist_px=dist_px)


def time_at(ptimes, hit):
    """The print time of a `pick_segment` hit, from the drawn paths' own
    per-point times; None when the picture carries none."""
    if ptimes is None or hit is None or hit["path"] >= len(ptimes):
        return None
    tp = np.asarray(ptimes[hit["path"]], np.float64)
    j = hit["seg"]
    if j + 1 >= len(tp):
        return None
    return float(tp[j] + hit["t"] * (tp[j + 1] - tp[j]))


def line_at_time(clock, tt):
    """The 1-based g-code line running at print time `tt` on the player's
    clock (`times` (P,) segment boundaries, `seg_line` (P-1,) the line of
    each segment). A moment on a boundary belongs to the segment that ENDS
    there unless it is the very start, and a zero-length segment (a G0
    marker, a dwell's instant) is never the answer while a moving one is."""
    times = np.asarray(clock["times"], np.float64)
    sl = np.asarray(clock["seg_line"])
    if len(sl) == 0:
        return None
    j = int(np.searchsorted(times, tt, side="left")) - 1
    j = min(max(j, 0), len(sl) - 1)
    # step off a zero-length segment onto the move that holds this instant
    while j > 0 and times[j + 1] <= times[j]:
        j -= 1
    while j < len(sl) - 1 and times[j + 1] <= times[j] \
            and times[j + 1] < tt:
        j += 1
    return int(sl[j])


def layer_at_line(nodes, line):
    """The outline's layer holding 1-based export `line`, as
    dict(scaffold, layer, first, last, key, label): `scaffold` the 1-based
    ordinal of its group among the outline's groups, `layer` 1-based, the
    lines 1-based and inclusive, `key` the outline key (piece, group, layer).
    A line inside a group but in no layer of it gives the group, layer None.
    None outside every group (header, exit)."""
    n0 = int(line) - 1
    g_ord = 0
    for nd in nodes:
        if nd.kind != "group":
            continue
        g_ord += 1
        if not nd.first <= n0 <= nd.last:
            continue
        for ch in nd.children:
            if ch.first <= n0 <= ch.last:
                return dict(scaffold=g_ord, layer=int(ch.key[2]) + 1,
                            first=ch.first + 1, last=ch.last + 1,
                            key=tuple(ch.key), line=int(line),
                            label="%s of %s" % (ch.label, nd.label))
        return dict(scaffold=g_ord, layer=None, first=nd.first + 1,
                    last=nd.last + 1, key=tuple(nd.key), line=int(line),
                    label=nd.label)
    return None


def line_window(clock, first, last):
    """(t0, t1): the print-time window lines `first`..`last` (1-based,
    inclusive) run in, or None when no move lies inside them."""
    times = np.asarray(clock["times"], np.float64)
    sl = np.asarray(clock["seg_line"])
    j0 = int(np.searchsorted(sl, first, side="left"))
    j1 = int(np.searchsorted(sl, last, side="right"))
    if j1 <= j0:
        return None
    return float(times[j0]), float(times[j1])


def layer_polylines(paths, ptimes, t0, t1):
    """The drawn fiber laid in print-time window [t0, t1], as (N,3) pieces,
    each cut at the window's ends by linear interpolation."""
    out = []
    if ptimes is None or t1 <= t0:
        return out
    for p, tp in zip(paths, ptimes):
        tp = np.asarray(tp, np.float64)
        if len(tp) < 2 or tp[-1] <= t0 or tp[0] >= t1:
            continue
        p = np.asarray(p, np.float64)
        i0 = int(np.searchsorted(tp, t0, side="right"))
        i1 = int(np.searchsorted(tp, t1, side="left"))
        piece = [p[i0:i1]]
        if 0 < i0 <= len(tp) - 1:
            a = (t0 - tp[i0 - 1]) / max(tp[i0] - tp[i0 - 1], 1e-300)
            piece.insert(0, (p[i0 - 1] + a * (p[i0] - p[i0 - 1]))[None])
        if 0 < i1 < len(tp):
            a = (t1 - tp[i1 - 1]) / max(tp[i1] - tp[i1 - 1], 1e-300)
            piece.append((p[i1 - 1] + a * (p[i1] - p[i1 - 1]))[None])
        q = np.concatenate(piece, axis=0)
        if len(q) >= 2:
            out.append(q)
    return out
