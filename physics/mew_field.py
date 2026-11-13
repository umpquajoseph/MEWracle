"""Electrostatic field for the melt-electrowriting (MEW) gap -- the `field(x)` seam.

The spatial-field counterpart to `mew_physics.py`: what is the electric field at a
point in the gap -- the thing a curved jet trajectory, and later a deposited-fibre
perturbation, actually read.

It is a SEAM: every consumer talks to the `Field` protocol (`potential`, `field`)
and nothing else. The only implementation today is closed-form (`AnalyticField`,
method of images); a real Laplace solve is a later drop-in behind the same
protocol, an accuracy upgrade rather than a new capability, because the analytic
field already hosts the qualitative physics as superposed sources.

Design intent mirrors `mew_physics`:
* DEFAULT TO THE ONE DEFENSIBLE LAW. With no sources `AnalyticField` is the
  parallel-plate gap field, E = V/H at the grounded collector -- the analog of CTS
  defaulting to `cts_ref` with zero exponents.
* RICHER EFFECTS ARE SUPERPOSED IMAGE-CHARGE SOURCES, defaulting to none: tip
  field-enhancement (a point source at the needle tip) and deposited-fibre
  repulsion (like-charged sources at laid fibres, the real cause of the MEW fibre-
  wall lateral instability). The repulsion plumbing exists and is unused until
  deposition is incremental and something switches it on.
* THE BOUNDARY CONDITION IS EXACT, NOT ITERATED. Every source carries its image in
  the grounded plane, so phi = 0 on the collector holds identically, and a
  superposition of harmonic terms is harmonic, so Laplace's equation holds by
  construction. Both are provable in a test, not a solver tolerance.

FRAME, which consumers must honour: the collector is the grounded plane z = 0, the
print direction is +z, and the needle tip sits on the axis at (0, 0, H) at the
applied potential. A consumer with world coordinates maps its query point into this
frame before sampling.

Units: length mm | voltage kV | potential kV | field kV/mm. A point source's
`strength` folds 1/(4*pi*eps0)*q into one fittable scalar in kV*mm, so
phi_point = strength / distance comes out in kV with no eps0 bookkeeping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, Tuple

Vec3 = Tuple[float, float, float]

# A query closer than this to a point source sits on the singularity, where the
# Coulomb field is undefined; we clamp the distance so a stray sample returns a
# large-but-finite value instead of raising. Consumers that integrate a field
# line should start just off a source, not on it.
EPS = 1.0e-9


def _dist(p: Vec3, q: Vec3) -> float:
    return math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2)


# --------------------------------------------------------------------------- #
# The seam: every consumer talks to this and nothing else.
# --------------------------------------------------------------------------- #
class Field(Protocol):
    """A static electric field in the gap. The whole point of the roadmap's
    `field(x)` interface: trajectory integrators, the jet renderer, and a future
    FEM solve all meet here."""

    def potential(self, p: Vec3) -> float:
        """Electrostatic potential phi at point p, in kV."""
        ...

    def field(self, p: Vec3) -> Vec3:
        """Electric field E = -grad(phi) at point p, in kV/mm."""
        ...


# --------------------------------------------------------------------------- #
# Superposable sources -- each carries its own image so the collector stays
# grounded. Default configuration has none of these (pure uniform gap field).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PointCharge:
    """A point source above the grounded collector, given WITH its image in the
    z = 0 plane so phi = 0 there holds exactly. `strength` is 1/(4*pi*eps0)*q
    folded into one fittable scalar (kV*mm): phi = strength/distance (kV).

    A positive strength placed at the needle tip models tip field-enhancement;
    sources placed at laid fibres model the deposited-fibre perturbation. Both
    default to absent -- the model asserts neither until data adds them."""
    height: float          # z of the source, mm (> 0, above the collector)
    strength: float        # kV*mm
    x: float = 0.0
    y: float = 0.0

    def potential(self, p: Vec3) -> float:
        src = (self.x, self.y, self.height)
        img = (self.x, self.y, -self.height)          # image: -strength below the plane
        ds = max(_dist(p, src), EPS)
        di = max(_dist(p, img), EPS)
        return self.strength * (1.0 / ds - 1.0 / di)

    def field(self, p: Vec3) -> Vec3:
        src = (self.x, self.y, self.height)
        img = (self.x, self.y, -self.height)
        ds = max(_dist(p, src), EPS)
        di = max(_dist(p, img), EPS)
        ks = self.strength / ds ** 3                  # E_src = +strength*(p-src)/|p-src|^3
        ki = self.strength / di ** 3                  # E_img = -strength*(p-img)/|p-img|^3
        return (
            ks * (p[0] - src[0]) - ki * (p[0] - img[0]),
            ks * (p[1] - src[1]) - ki * (p[1] - img[1]),
            ks * (p[2] - src[2]) - ki * (p[2] - img[2]),
        )


# --------------------------------------------------------------------------- #
# The one implementation today: closed-form, method of images.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AnalyticField:
    """Parallel-plate gap field plus optional superposed image-charge sources.

    With `sources` empty (the default) this is exactly the uniform field a
    grounded-collector / charged-tip gap produces to first order:

        phi(x, y, z) = voltage * z / gap        (kV; phi = 0 at collector, = V at tip)
        E            = (0, 0, -voltage / gap)   (kV/mm; points at the collector)

    Every added source contributes its image-paired Coulomb term, so the sum
    stays harmonic and the collector stays grounded. This is the analytic stand-in
    a FEM solve later replaces behind the `Field` protocol."""
    voltage: float                                    # tip potential, kV
    gap: float                                        # working distance H, mm
    sources: Tuple[PointCharge, ...] = ()

    def __post_init__(self) -> None:
        if self.gap <= 0.0:
            raise ValueError("gap must be positive (mm)")

    def potential(self, p: Vec3) -> float:
        phi = self.voltage * p[2] / self.gap
        for s in self.sources:
            phi += s.potential(p)
        return phi

    def field(self, p: Vec3) -> Vec3:
        ex, ey, ez = 0.0, 0.0, -self.voltage / self.gap
        for s in self.sources:
            sx, sy, sz = s.field(p)
            ex += sx
            ey += sy
            ez += sz
        return (ex, ey, ez)


def field_magnitude(e: Vec3) -> float:
    """|E| -- a small convenience for consumers and tests."""
    return math.sqrt(e[0] * e[0] + e[1] * e[1] + e[2] * e[2])


def field_direction(e: Vec3) -> Vec3:
    """Unit vector along E; (0, 0, -1) for a degenerate zero field so a consumer
    that follows the field line still gets a sane downward default."""
    m = field_magnitude(e)
    if m <= EPS:
        return (0.0, 0.0, -1.0)
    return (e[0] / m, e[1] / m, e[2] / m)


__all__ = [
    "Vec3", "Field", "PointCharge", "AnalyticField",
    "field_magnitude", "field_direction",
]
