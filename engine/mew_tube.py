"""Tube design relations -- McColl 2018, Eqs 1.1-6.1. A tube's parameters are NOT free.

McColl, Cornock, Ryan, Cameron & Ryan et al., "Design and fabrication of melt
electrowritten tubes using intuitive software", Materials & Design 155 (2018)
46-58 [S58], MEWTubes. Pure math: stdlib only, no numpy, no VTK, no I/O.

WHY THIS EXISTS. At a random radius, winding angle and length the helix does not
close: the fibre misses its own start, the second layer lands between the first
layer's fibres and the wall never registers. McColl's Fig 2 shows 3.24 mm
(closes) against 3.25 mm (not), so the page must SOLVE these, not collect them.

CONVENTIONS, agreeing across all three mandrel sources we hold. The WINDING
ANGLE is measured FROM THE MANDREL AXIS, 0 deg along the axis and 90 deg a ring;
McColl's omega, Neuhaus's alpha and `mew_frame`'s psi are the same angle, which
is why `normal_curvature` takes psi from u. UNITS are MEWracle's (mm, mm/s,
degrees); McColl's rpm / mm/min in Eq 1.2 converts in `tangential_speed_from_rpm`.

THE EQUATIONS, transcribed from the paper (not from a summary):

    1.1  omega   = arctan(V_tang / V_trans)
    1.2  V_tang  = r * V_rot * 2*pi                 (V_rot rpm -> mm/min)
    1.3  V_eff   = sqrt(V_trans^2 + V_tang^2)
    2.1  lambda  = 2*pi*r / tan(omega)              the helix PITCH length
    3.1  L       = lambda * (Fm + Rm)
    4.1  Fm      = 1/eta,  eta a whole-number divisor of 360
    4.2  N       = eta + eta*(eta mod 2)            pivot points, forced EVEN
    4.3  alpha   = 360 / N                          the pivot angle
    5.1  LA      = 2*pi*r * (2*alpha/360)           pore arc length
    5.2  HT      = 0.5 * LA / tan(omega)            pore triangle height
    5.3  Pore    = HT * LA

A table of only 1.1-1.3, 2.1 and 4.1-4.3 omits
the one equation that produces a length and cannot be implemented as printed --
read the paper. Eq 6.1, the parametric helix with its `(-1)^Li` flip and
`(Li + Li mod 2)` offset, is the GENERATOR's business and lives in
`scaffold_gcode`. Eq 1.3 is why the scalar speed the diameter and CTS laws
consume is the RESULTANT on a 4-axis machine, not the axial word. VALIDATED
against McColl's published Table 2 -- a 1.5 mm-diameter mandrel at a 20 deg
winding angle, "a fixed pitch length of 12.94 mm", eta = 2, 3, 4, 6."""

import math

#: The whole-number divisors of 360. Eq 4.1's text says eta must be one of
#: these -- **and McColl's own experiments violate that**, so we do not enforce
#: it. See `_check_eta`. Kept because a divisor makes the pivot angle a whole
#: number of degrees, which is a nicety worth being able to offer.
DIVISORS_360 = tuple(n for n in range(1, 361) if 360 % n == 0)

#: The largest eta the length ladder is searched over by default. 36 is already
#: 36 pivot points; McColl's own prints used 6 to 18.
DEFAULT_MAX_ETA = 36


class TubeDesignError(ValueError):
    """A (radius, winding angle, length) triple that cannot produce a closing tube."""


# --------------------------------------------------------------------------- #
# Eqs 1.1-1.3 -- the velocity triangle
# --------------------------------------------------------------------------- #
def winding_angle_deg(tangential_speed, translation_speed):
    """Eq 1.1 -- the winding angle from the two axis speeds, degrees FROM THE AXIS."""
    return math.degrees(math.atan2(tangential_speed, translation_speed))


def tangential_speed_from_rpm(radius, rpm):
    """Eq 1.2 -- surface speed in MM/S from mandrel rpm.

    McColl writes `V_tang = r * V_rot * 2*pi` with V_rot in rpm, giving mm/MIN.
    The /60 to mm/s is applied here and nowhere else.
    """
    return radius * rpm * 2.0 * math.pi / 60.0


def rpm_from_tangential_speed(radius, tangential_speed):
    """Eq 1.2 inverted -- the rpm that gives this surface speed, mm/s in."""
    if radius <= 0.0:
        raise TubeDesignError("mandrel radius must be positive, got %r" % (radius,))
    return tangential_speed * 60.0 / (radius * 2.0 * math.pi)


def effective_speed(translation_speed, tangential_speed):
    """Eq 1.3 -- the RESULTANT the jet actually sees. What CTS and the diameter
    law consume on a 4-axis machine; see the module docstring."""
    return math.hypot(translation_speed, tangential_speed)


def axis_speeds(radius, omega_deg, effective):
    """Eq 1.1 + 1.3 inverted: the axis speeds giving this winding angle at this
    effective speed. Returns `(translation mm/s, tangential mm/s, rpm)`.

    This is the direction the GUI needs -- a user picks a winding angle and an
    effective speed (the one that has to sit above CTS), not two axis speeds.
    McColl's own application presents "a table showing many possible
    rotational/translational combinations and their associated effective
    velocity", and notes the choice "will require trial and error on the given
    machine". Ours is the same choice made from the other end.
    """
    w = math.radians(omega_deg)
    v_trans = effective * math.cos(w)
    v_tang = effective * math.sin(w)
    return v_trans, v_tang, rpm_from_tangential_speed(radius, v_tang)


# --------------------------------------------------------------------------- #
# Eqs 2.1, 3.1, 4.1-4.3 -- pitch, pivots and the lengths that CLOSE
# --------------------------------------------------------------------------- #
def _check_helix(radius, omega_deg):
    if not radius > 0.0:
        raise TubeDesignError("mandrel radius must be positive, got %r" % (radius,))
    if not 0.0 < omega_deg < 90.0:
        raise TubeDesignError(
            "winding angle must be strictly between 0 and 90 deg (got %r): at 0 "
            "the fiber runs along the axis and never wraps, so the pitch is "
            "infinite; at 90 it is a closed ring and the pitch is zero. Neither "
            "is a helix and neither has pivot points." % (omega_deg,))


def pitch_length(radius, omega_deg):
    """Eq 2.1 -- the axial distance one full helical revolution advances."""
    _check_helix(radius, omega_deg)
    return 2.0 * math.pi * radius / math.tan(math.radians(omega_deg))


def _check_eta(eta):
    """`eta` must be a positive INTEGER -- not, as Eq 4.1 says, a divisor of 360.

    THE PAPER CONTRADICTS ITSELF HERE, and we follow what it PRINTED rather than
    what it wrote. Eq 4.1's text reads "eta is a whole number divisor of 360",
    but §2.6 tested "Pivot points of 12, 14, 16 and 18" and §2.6 again selected
    "16-pivot points for 20 deg, 14 for 30 deg". N = 14 needs eta = 14 and
    N = 16 needs eta = 16 (both even, so Eq 4.2 leaves them alone) -- and
    360/14 = 25.714, 360/16 = 22.5. Neither divides 360.

    The geometry does not need the constraint either: a layer closes when some
    EVEN number of length iterations completes a whole number of revolutions,
    and k = eta (or 2*eta when eta is odd) does that for ANY integer eta. All
    the divisor buys is a pivot angle that is a whole number of degrees, which
    is a display nicety, not a manufacturing one.

    Enforcing Eq 4.1 as written would refuse two of the four pivot counts McColl
    actually printed -- a validator rejecting its own source's experiments.
    """
    if not isinstance(eta, int) or isinstance(eta, bool) or eta < 1:
        raise TubeDesignError(
            "eta must be a positive whole number, got %r" % (eta,))


def eta_gives_whole_degree_pivots(eta):
    """Does this eta land the pivot angle on a whole number of degrees?

    True exactly when Eq 4.1's stated divisor condition holds. Offered so a UI
    can PREFER those without refusing the others (see `_check_eta`).
    """
    return pivot_points(eta) in _DIVISOR_SET


def fraction_multiplier(eta):
    """Eq 4.1 -- the fraction of a revolution one length iteration completes."""
    _check_eta(eta)
    return 1.0 / eta


def pivot_points(eta):
    """Eq 4.2 -- `N = eta + eta*(eta mod 2)`; forced EVEN so the pairs complete.

    An odd eta DOUBLES: eta = 3 gives N = 6, which is why Table 2 lists eta 3 and
    eta 6 with the same N, the same pivot angle and the same pore area but
    DIFFERENT lengths (4.32 vs 2.16 mm at Rm = 0).
    """
    _check_eta(eta)
    return eta + eta * (eta % 2)


def pivot_angle_deg(n_pivots):
    """Eq 4.3 -- the mandrel rotation between consecutive pivot points."""
    if n_pivots <= 0:
        raise TubeDesignError("pivot points must be positive, got %r" % (n_pivots,))
    return 360.0 / n_pivots


def tube_length(pitch, fm, rm=0):
    """Eq 3.1 -- `L = lambda * (Fm + Rm)`.

    `Rm` is a whole number of EXTRA revolutions, so one (radius, winding angle,
    eta) admits an infinite ladder of valid lengths, not one. McColl: "for a
    fixed pitch length there exists an array of valid lengths available".
    """
    if rm < 0 or rm != int(rm):
        raise TubeDesignError("Rm must be a non-negative whole number, got %r" % (rm,))
    return pitch * (fm + int(rm))


_DIVISOR_SET = frozenset(DIVISORS_360)


# --------------------------------------------------------------------------- #
# Eqs 5.1-5.3 -- the theoretical pore
# --------------------------------------------------------------------------- #
def pore_arc_length(radius, pivot_angle):
    """Eq 5.1 -- `LA = 2*pi*r * (2*alpha/360)`, the circumferential distance
    between the two pore corners perpendicular to the tube length."""
    return 2.0 * math.pi * radius * (2.0 * pivot_angle / 360.0)


def pore_triangle_height(arc_length, omega_deg):
    """Eq 5.2 -- `HT = 0.5*LA / tan(omega)`."""
    _check_helix(1.0, omega_deg)
    return 0.5 * arc_length / math.tan(math.radians(omega_deg))


def pore_area(radius, pivot_angle, omega_deg):
    """Eq 5.3 -- the diamond pore's area, mm^2. Goes as 1/N^2, so halving the
    pore's linear size costs four times the pivot points."""
    la = pore_arc_length(radius, pivot_angle)
    return pore_triangle_height(la, omega_deg) * la


# --------------------------------------------------------------------------- #
# The design solver -- what the parameter page actually calls
# --------------------------------------------------------------------------- #
class TubeDesign(object):
    """One CLOSING tube: a (radius, winding angle) with an eta, Rm and length.

    Every field is derived; nothing here is a free choice except `eta` and `rm`.
    """

    __slots__ = ("radius", "omega_deg", "eta", "rm", "pitch", "fm",
                 "n_pivots", "pivot_angle", "length", "pore_area",
                 "pass_rev", "end_turn_deg")

    def __init__(self, radius, omega_deg, eta, rm=0):
        self.radius = float(radius)
        self.omega_deg = float(omega_deg)
        self.pitch = pitch_length(radius, omega_deg)
        self.eta = int(eta)
        self.rm = int(rm)
        self.fm = fraction_multiplier(self.eta)
        self.n_pivots = pivot_points(self.eta)
        self.pivot_angle = pivot_angle_deg(self.n_pivots)
        self.length = tube_length(self.pitch, self.fm, self.rm)
        self.pore_area = pore_area(self.radius, self.pivot_angle, self.omega_deg)
        # What the generator reads: revolutions per pass, and the pure turn at
        # the end of each pass. Eq 3.1 closes by LENGTH, so there is no turn.
        self.pass_rev = self.fm + self.rm
        self.end_turn_deg = 0.0

    def __repr__(self):
        return ("TubeDesign(r=%.3f, omega=%.1f deg, eta=%d, Rm=%d -> N=%d, "
                "alpha=%.2f deg, L=%.3f mm, pore=%.4f mm^2)"
                % (self.radius, self.omega_deg, self.eta, self.rm,
                   self.n_pivots, self.pivot_angle, self.length,
                   self.pore_area))


class TurnedTubeDesign(object):
    """A tube that closes by TURNING at each end, not by its length.

    McColl's Eq 3.1 closes the helix by length, so on one rod at one winding
    angle only a ladder of lengths closes -- 3 mm at 36 deg closes near 20 mm
    only with N = 2, one 7.8 mm diamond. Here the length and the angle are
    exactly what was asked, and after each pass the mandrel turns, with no
    axial move, until the next pass starts on the next pivot. That turn lays a
    short ring of fibre at the tube's end; it is never longer than a few pivot
    angles, and is zero when the length happens to close on its own.

    THE CLOSURE RULE, which `TubeDesign` also obeys: a pass (axial run plus
    end turn) advances `m / N` revolutions, N even. After N passes the head
    is back at the near end and a whole `m` revolutions on, and the N/2
    forward passes start on N/2 evenly spaced angles exactly when
    gcd(m, N/2) == 1. McColl's even eta is m = 1 + eta*Rm; odd eta is
    m = 2*(1 + eta*Rm) over N = 2*eta. `m` here is the smallest that is
    coprime and does not turn BACKWARDS, so the turn is as short as it can be.

    The pore is Eq 5.3 at this N and omega, unchanged: two families of evenly
    spaced parallel helices cross in the same diamond wherever their phase
    sits, and the turn only moves that phase. `eta` is N (an even eta gives
    N = eta) and `rm` is m // N, so the page's (eta, Rm, omega) pin key and
    every reader of `n_pivots` / `pivot_angle` / `pore_area` work unchanged.
    """

    __slots__ = ("radius", "omega_deg", "eta", "rm", "pitch", "fm",
                 "n_pivots", "pivot_angle", "length", "pore_area",
                 "pass_rev", "end_turn_deg", "m")

    def __init__(self, radius, omega_deg, n_pivots, length):
        _check_helix(radius, omega_deg)
        n = int(n_pivots)
        if n < 2 or n % 2 or n != n_pivots:
            raise TubeDesignError(
                "pivot points must be an even whole number of at least 2, got "
                "%r: the pattern's out and back passes come in pairs (Eq 4.2)"
                % (n_pivots,))
        if not length > 0.0:
            raise TubeDesignError("tube length must be positive, got %r"
                                  % (length,))
        self.radius = float(radius)
        self.omega_deg = float(omega_deg)
        self.pitch = pitch_length(radius, omega_deg)
        self.length = float(length)
        self.n_pivots = n
        self.pivot_angle = pivot_angle_deg(n)
        self.pore_area = pore_area(self.radius, self.pivot_angle,
                                   self.omega_deg)
        run_rev = self.length / self.pitch      # what the axial run turns
        # the smallest m with m / N >= run_rev, up to float noise, and coprime
        # to N/2; one always exists within N/2 steps
        m = max(1, int(math.ceil(run_rev * n - 1e-9)))
        while math.gcd(m, n // 2) != 1:
            m += 1
        self.m = m
        self.pass_rev = m / float(n)
        self.end_turn_deg = max(0.0, 360.0 * (self.pass_rev - run_rev))
        self.eta = n
        self.rm = m // n
        self.fm = 1.0 / n

    def __repr__(self):
        return ("TurnedTubeDesign(r=%.3f, omega=%.1f deg, N=%d, m=%d, "
                "L=%.3f mm, end turn %.2f deg, pore=%.4f mm^2)"
                % (self.radius, self.omega_deg, self.n_pivots, self.m,
                   self.length, self.end_turn_deg, self.pore_area))


def pore_side_um(design):
    """The pore as the side of a square of equal area, in micrometres -- how
    the page states it."""
    return 1000.0 * math.sqrt(design.pore_area)


def solve_turned_tube(radius, omega_deg, length, target_pore_area, *,
                      fiber_diameter=None, max_pivots=360, n_alternatives=4):
    """Winding angle, pore and length in; the tube that honours all three.

    Returns `(TurnedTubeDesign, [TurnedTubeDesign, ...])`. Angle and length
    come back exactly as asked. The pore is the nearest of the even pivot
    counts, by `|log|` of the pore SIDE -- the pore goes as 1/N, so only the
    two even N bracketing the target can be nearest. The alternatives are the
    neighbouring even N either side, coarsest first, for a page's ladder.

    `fiber_diameter` drops an N whose diamond the fibre itself closes (Eq 5.1
    LA or twice Eq 5.2 HT below one fibre width), as `solve_tube_for_pore`
    does. Raises `TubeDesignError` when nothing is left."""
    _check_helix(radius, omega_deg)
    if not target_pore_area > 0.0:
        raise TubeDesignError("pore size must be positive, got %r mm^2"
                              % (target_pore_area,))
    if not length > 0.0:
        raise TubeDesignError("tube length must be positive, got %r"
                              % (length,))
    fiber = None if fiber_diameter is None else float(fiber_diameter)

    def open_(n):
        la = pore_arc_length(radius, pivot_angle_deg(n))
        return fiber is None or (
            la >= fiber and 2.0 * pore_triangle_height(la, omega_deg) >= fiber)

    # the pore side is k / N; read k off N = 2 rather than re-deriving Eq 5.3
    k = 2.0 * math.sqrt(pore_area(radius, pivot_angle_deg(2), omega_deg))
    n_star = k / math.sqrt(target_pore_area)
    lo = max(2, 2 * int(math.floor(n_star / 2.0)))
    cands = [n for n in (lo, lo + 2) if n <= max_pivots and open_(n)]
    if not cands:
        cands = [n for n in range(2, int(max_pivots) + 1, 2) if open_(n)]
        if not cands:
            raise TubeDesignError(
                "the %.3f mm fiber closes every pore on this mandrel at %.1f "
                "deg: Eq 5.1's LA or 2 x Eq 5.2's HT is below one fiber width "
                "even at 2 pivot points" % (fiber, omega_deg))

    def side_err(n):
        return abs(math.log(k / n / math.sqrt(target_pore_area)))

    best = min(cands, key=lambda n: (side_err(n), n))
    alts = []
    step = 2
    while len(alts) < n_alternatives and step <= max_pivots:
        for n in (best - step, best + step):
            if 2 <= n <= max_pivots and open_(n) and len(alts) < n_alternatives:
                alts.append(n)
        step += 2
    return (TurnedTubeDesign(radius, omega_deg, best, length),
            [TurnedTubeDesign(radius, omega_deg, n, length)
             for n in sorted(alts)])


def valid_lengths(radius, omega_deg, max_length=None, max_rm=2, etas=None):
    """Every closing length up to `max_length` -- McColl's Table 2, generated.

    Sorted by length. `etas` defaults to every integer up to `DEFAULT_MAX_ETA` --
    NOT only the divisors of 360, because McColl printed eta = 14 and 16 and
    neither divides 360 (see `_check_eta`).
    """
    _check_helix(radius, omega_deg)
    if etas is None:
        etas = list(range(1, DEFAULT_MAX_ETA + 1))
    out = []
    for eta in etas:
        for rm in range(int(max_rm) + 1):
            d = TubeDesign(radius, omega_deg, eta, rm)
            if max_length is None or d.length <= max_length + 1e-12:
                out.append(d)
    out.sort(key=lambda d: d.length)
    return out


def solve_tube(radius, omega_deg, target_length, max_rm=64, etas=None,
               max_relative_error=None):
    """The closing tube NEAREST a target length. Raises rather than approximating
    silently when `max_relative_error` is given and nothing gets close enough.

    This is the call the mandrel parameter page makes: the user asks for "a 10 mm
    tube at 45 degrees on a 3 mm mandrel" and gets back the length that actually
    closes, plus the pivot count and pivot angle the generator needs.
    """
    _check_helix(radius, omega_deg)
    if not target_length > 0.0:
        raise TubeDesignError("target length must be positive, got %r"
                              % (target_length,))
    if etas is None:
        etas = list(range(1, DEFAULT_MAX_ETA + 1))
    pitch = pitch_length(radius, omega_deg)
    best = None
    for eta in etas:
        fm = 1.0 / eta
        # the Rm bracketing the target for this eta -- no need to scan the ladder
        rm_star = target_length / pitch - fm
        for rm in {0, int(math.floor(rm_star)), int(math.ceil(rm_star))}:
            if rm < 0 or rm > max_rm:
                continue
            d = TubeDesign(radius, omega_deg, eta, rm)
            err = abs(d.length - target_length)
            if best is None or err < best[0] or (
                    err == best[0] and d.n_pivots < best[1].n_pivots):
                best = (err, d)
    if best is None:
        raise TubeDesignError(
            "no closing length exists for r=%r, omega=%r within Rm<=%d"
            % (radius, omega_deg, max_rm))
    err, design = best
    if max_relative_error is not None and err / target_length > max_relative_error:
        raise TubeDesignError(
            "no closing tube within %.3g of %.4f mm at r=%.4f, omega=%.2f deg; "
            "nearest is %.4f mm (eta=%d, Rm=%d, %.2f%% away). A length that does "
            "not close leaves the helix out of sync, so the second layer lands "
            "between the first layer's fibers instead of on them."
            % (max_relative_error, target_length, radius, omega_deg,
               design.length, design.eta, design.rm,
               100.0 * err / target_length))
    return design


def nearest_closing_lengths(radius, omega_deg, target_length,
                            max_eta=DEFAULT_MAX_ETA, max_rm=64):
    """`(shorter, longer)`: the closing tubes nearest `target_length` from
    below and from above, over `solve_tube`'s eta / Rm range. Either is None
    when nothing closes on that side (nothing is shorter than lambda / max_eta).
    What a page offers when `solve_tube` refuses a length -- the gap between
    `Fm = 1` and `Fm = 1/2` at one Rm is a real hole in the ladder."""
    _check_helix(radius, omega_deg)
    pitch = pitch_length(radius, omega_deg)
    below = above = None
    for eta in range(1, int(max_eta) + 1):
        rm_star = target_length / pitch - 1.0 / eta
        for rm in {0, int(math.floor(rm_star)), int(math.ceil(rm_star))}:
            if rm < 0 or rm > max_rm:
                continue
            d = TubeDesign(radius, omega_deg, eta, rm)
            if d.length <= target_length and (
                    below is None or d.length > below.length):
                below = d
            if d.length >= target_length and (
                    above is None or d.length < above.length):
                above = d
    return below, above


def solve_tube_options(radius, omega_deg, target_length, *, length_tol=0.05,
                       max_eta=DEFAULT_MAX_ETA, max_rm=64, fiber_diameter=None,
                       n_alternatives=6):
    """The winding-angle page's closure: `solve_tube`'s pick, plus the OTHER
    pore sizes this angle closes at near the requested length.

    Returns `(TubeDesign, [TubeDesign, ...])`. The pick IS `solve_tube` over
    the same eta and Rm range at `max_relative_error=length_tol`, so at the
    default 5 % it is exactly the design `scaffold_gcode.mandrel_design` makes
    when no eta is pinned (the CLI's own answer), and it raises the same way.

    With omega fixed, eta and Rm only move the length and N, and N alone sets
    the pore (Eqs 4.2-5.3). So the alternatives are one row per pivot count:
    the nearest-length (eta, Rm) for each N inside `length_tol`, dropping the
    pick's own N, rows whose pore the fibre closes (Eq 5.1 LA or twice Eq 5.2
    HT below `fiber_diameter`), and then thinned evenly to `n_alternatives`,
    so the ladder spans the pore range instead of listing neighbours. Sorted
    by N, coarsest pore first."""
    best = solve_tube(radius, omega_deg, target_length, max_rm=max_rm,
                      etas=range(1, int(max_eta) + 1),
                      max_relative_error=length_tol)
    pitch = pitch_length(radius, omega_deg)
    fiber = None if fiber_diameter is None else float(fiber_diameter)
    tol_abs = length_tol * target_length + 1e-12
    by_n = {}
    for eta in range(1, int(max_eta) + 1):
        rm_star = target_length / pitch - 1.0 / eta
        for rm in sorted({0, int(math.floor(rm_star)),
                          int(math.ceil(rm_star))}):
            if rm < 0 or rm > max_rm:
                continue
            d = TubeDesign(radius, omega_deg, eta, rm)
            err = abs(d.length - target_length)
            if err > tol_abs or d.n_pivots == best.n_pivots:
                continue
            if fiber is not None:
                la = pore_arc_length(radius, d.pivot_angle)
                if la < fiber or 2.0 * pore_triangle_height(la, omega_deg) < fiber:
                    continue
            prev = by_n.get(d.n_pivots)
            if prev is None or err < prev[0]:
                by_n[d.n_pivots] = (err, d)
    rows = [by_n[n][1] for n in sorted(by_n)]
    n = int(n_alternatives)
    if len(rows) > n > 1:
        rows = [rows[int(round(i * (len(rows) - 1) / (n - 1.0)))]
                for i in range(n)]
    elif len(rows) > n:
        rows = rows[:max(n, 0)]
    return best, rows


def solve_tube_for_pore(radius, target_length, target_pore_area, *,
                        length_tol=0.05,            # fraction of target_length
                        max_pore_rel_error=0.25,    # fraction, on the AREA
                        omega_range=(15.0, 75.0), omega_step=0.5,
                        max_eta=120, max_rm=64,
                        fiber_diameter=None,
                        n_alternatives=6, pore_band=0.01):
    """The closing tube whose PORE is nearest a target, subject to length.

    Returns `(TubeDesign, [TubeDesign, ...])` -- the pick, then an alternatives
    ladder -- and raises `TubeDesignError` rather than approximating silently.
    `target_pore_area` is mm^2 (a page collecting a pore SIDE squares it once);
    `length_tol` / `max_pore_rel_error` are fractions of length / target AREA.

    THE SEARCH. omega over `omega_range` at `omega_step`; the 15-75 deg default
    is a default, not a law (`_check_helix` forbids only 0 and 90), outside which
    the pore has degenerated into a slit. eta over 1..`max_eta` (120, where
    `DEFAULT_MAX_ETA` is 36) because Eq 5.3 goes as 1/N^2; Rm is NOT scanned,
    `solve_tube`'s bracketing being reused verbatim.

    THE OBJECTIVE, in order: feasible on `length_tol`; the physical floor, which
    drops a candidate whose Eq 5.1 LA or twice Eq 5.2 HT is below
    `fiber_diameter`; the pore SIDE error `|log(sqrt(pore/target))|`, symmetric
    and QUANTISED into `pore_band` (1 %) bands; then within a band the smaller
    length error, fewer pivot points, omega nearest 45 deg. Banding is
    load-bearing -- unbanded, r = 1.5 mm / 12 mm / 400 um picks omega 66.5 deg /
    eta 22 (+4.0 % of length) over omega 58.5 deg / eta 13 (-0.0 %) -- and rides
    the SIDE because an area error is twice a side error.

    THE LADDER spans the trade instead of listing near-duplicates: the best-pore
    design in each length-error bucket (0 %, <=1 %, <=3 %, <=`length_tol`), then
    next-best overall, deduped on `(eta, rm, round(omega, 3))`."""
    if not radius > 0.0:
        raise TubeDesignError("mandrel radius must be positive, got %r" % (radius,))
    if not target_length > 0.0:
        raise TubeDesignError("target length must be positive, got %r"
                              % (target_length,))
    if not target_pore_area > 0.0:
        raise TubeDesignError("target pore area must be positive, got %r mm^2"
                              % (target_pore_area,))
    lo, hi = float(omega_range[0]), float(omega_range[1])
    if not 0.0 < lo <= hi < 90.0:
        raise TubeDesignError(
            "the winding-angle range must satisfy 0 < low <= high < 90 deg "
            "(got %r): at 0 the fibre never wraps and at 90 it is a ring, so "
            "neither end has a pore." % (omega_range,))
    if not omega_step > 0.0:
        raise TubeDesignError("omega step must be positive, got %r" % (omega_step,))
    max_eta = int(max_eta)
    if max_eta < 1:
        raise TubeDesignError("the eta cap must be at least 1, got %r" % (max_eta,))
    if not pore_band > 0.0:
        raise TubeDesignError("the pore band must be positive, got %r" % (pore_band,))
    fiber = None if fiber_diameter is None else float(fiber_diameter)

    tol_abs = length_tol * target_length
    n_steps = int(math.floor((hi - lo) / omega_step + 1e-9))
    # Each row: (sort key, side error, pore area, omega, eta, Rm, length).
    # A row is a candidate, NOT a TubeDesign -- the design is built only for the
    # handful actually returned, and from the same (radius, omega, eta, Rm), so
    # what comes back is a real TubeDesign and not a struct that can drift.
    survivors = []
    nearest_any = None      # nearest closing length, tolerance and floor ignored
    n_feasible = 0          # inside the tolerance, floor ignored
    for i in range(n_steps + 1):
        omega = lo + i * omega_step
        pitch = pitch_length(radius, omega)                 # Eq 2.1
        for eta in range(1, max_eta + 1):
            fm = 1.0 / eta                                  # Eq 4.1
            n_pivots = eta + eta * (eta % 2)                # Eq 4.2
            alpha = 360.0 / n_pivots                        # Eq 4.3
            la = pore_arc_length(radius, alpha)             # Eq 5.1
            ht = pore_triangle_height(la, omega)            # Eq 5.2
            pore = ht * la                                  # Eq 5.3
            floor_ok = fiber is None or (la >= fiber and 2.0 * ht >= fiber)
            rm_star = target_length / pitch - fm
            for rm in sorted({0, int(math.floor(rm_star)),
                              int(math.ceil(rm_star))}):
                if rm < 0 or rm > max_rm:
                    continue
                length = pitch * (fm + rm)                  # Eq 3.1
                err = abs(length - target_length)
                if nearest_any is None or err < nearest_any[0]:
                    nearest_any = (err, omega, eta, rm, length)
                if err > tol_abs:
                    continue
                n_feasible += 1
                if not floor_ok:
                    continue
                side_err = abs(math.log(math.sqrt(pore / target_pore_area)))
                survivors.append(
                    ((int(side_err / pore_band), err, n_pivots,
                      abs(omega - 45.0)),
                     side_err, pore, omega, eta, rm, length))

    if nearest_any is None:
        raise TubeDesignError(
            "no closing tube exists at all for r = %.4f mm over omega %.1f-%.1f "
            "deg with eta <= %d and Rm <= %d" % (radius, lo, hi, max_eta, max_rm))
    if not n_feasible:
        err, w_n, eta_n, rm_n, len_n = nearest_any
        raise TubeDesignError(
            "no closing length within %.3g %% of %.4f mm at r = %.4f mm over "
            "omega %.1f-%.1f deg (eta <= %d, Rm <= %d); the nearest closes at "
            "%.4f mm (omega %.1f deg, eta %d, Rm %d), %.3g %% away. A length "
            "that does not close leaves the helix out of sync, so the second "
            "layer lands between the first layer's fibres instead of on them."
            % (100.0 * length_tol, target_length, radius, lo, hi, max_eta,
               max_rm, len_n, w_n, eta_n, rm_n, 100.0 * err / target_length))
    if not survivors:
        raise TubeDesignError(
            "the %.3f mm fibre leaves no pore at all in any of the %d closing "
            "lengths within %.3g %% of %.4f mm: Eq 5.1's LA or 2 x Eq 5.2's HT "
            "is below one fibre width in every one of them, so the two walls "
            "meet. Lower the eta cap, widen the length tolerance, or use a "
            "larger mandrel."
            % (fiber, n_feasible, 100.0 * length_tol, target_length))

    smallest = min(row[2] for row in survivors)
    nearest_pore = min(survivors, key=lambda row: row[1])
    survivors.sort(key=lambda row: row[0])
    best = survivors[0]
    rel_near = abs(nearest_pore[2] - target_pore_area) / target_pore_area
    rel_best = abs(best[2] - target_pore_area) / target_pore_area
    if rel_near > max_pore_rel_error or rel_best > max_pore_rel_error:
        if fiber is not None and target_pore_area < smallest:
            raise TubeDesignError(
                "a %.1f um pore is smaller than the %.3f mm fibre that bounds "
                "it: the pore diamond's diagonals are Eq 5.1's LA and 2 x "
                "Eq 5.2's HT, and below one fibre width the two walls meet. "
                "The smallest pore this mandrel and fibre admit is %.1f um "
                "(%.6f mm^2). Use a finer fiber or a larger mandrel; the pore "
                "you asked for is not clamped."
                % (1000.0 * math.sqrt(target_pore_area), fiber,
                   1000.0 * math.sqrt(smallest), smallest))
        raise TubeDesignError(
            "nearest pore is %.1f um (%.6f mm^2, omega %.1f deg, eta %d, "
            "Rm %d), %.3g %% from the %.1f um asked for on area -- more than "
            "the %.3g %% allowed. Widen the length tolerance, raise the eta "
            "cap, or change the mandrel."
            % (1000.0 * math.sqrt(nearest_pore[2]), nearest_pore[2],
               nearest_pore[3], nearest_pore[4], nearest_pore[5],
               100.0 * rel_near, 1000.0 * math.sqrt(target_pore_area),
               100.0 * max_pore_rel_error))

    def _ident(row):
        return (row[4], row[5], round(row[3], 3))

    seen = {_ident(best)}
    picked = []
    for bucket in (0.0, 0.01, 0.03, length_tol):
        for row in survivors:
            if len(picked) >= n_alternatives:
                break
            if row[0][1] <= bucket * target_length + 1e-12 \
                    and _ident(row) not in seen:
                seen.add(_ident(row))
                picked.append(row)
                break
    for row in survivors:
        if len(picked) >= n_alternatives:
            break
        if _ident(row) not in seen:
            seen.add(_ident(row))
            picked.append(row)
    return (TubeDesign(radius, best[3], best[4], best[5]),
            [TubeDesign(radius, r[3], r[4], r[5]) for r in picked])


def closes(radius, omega_deg, length, relative_tolerance=1e-3, max_rm=64):
    """Does this (radius, winding angle, length) triple close? -> (bool, nearest)."""
    d = solve_tube(radius, omega_deg, length, max_rm=max_rm)
    return abs(d.length - length) <= relative_tolerance * length, d
