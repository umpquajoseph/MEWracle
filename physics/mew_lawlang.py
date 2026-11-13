#!/usr/bin/env python3
"""mew_lawlang.py -- let a user REPLACE a physical law with their own expression.

**Why a language and not more knobs.** Every fittable hook in this build is the
same shape: `value = anchor * PROD (input/ref)^exp`, exponent 0 meaning off. That
covers a power-law dependence and nothing else. A user whose polymer needs a
logarithm, a saturating term, a threshold, or a coefficient this build has never
heard of has no way in. This module is the way in: the seven scalar laws (six
flat, plus the mandrel's circumferential lag `rcir`) accept a
typed expression over named variables, plus the user's own named coefficients.

**ONE VOCABULARY, THE SAME ON EVERY CARD.** `QUANTITIES` is the whole set of
live quantities and every law argument has to be in it, `CONTEXT_NAMES` is built
FROM it, so a law must not invent a second spelling for something that
already has a name. This is a real defect and not tidiness; once,
print speed was `v` in the diameter law and `speed` in the process context;
voltage was `V` in CTS and `voltage` in the context; and `eta` (absolute
viscosity, Pa.s) sat one letter from `visc`, a DIMENSIONLESS ratio.

Every quantity is usable in EVERY law, which is the half that was missing: the
Fiber-diameter card offered no `eta` because eta is not one of that law's own
arguments, so the reference list under each card said something different and a
user had to find the difference by trial. The five quantities no law is handed
directly are DERIVED -- `MEWModel._law_scope` computes them from the process
context when the expression names one, and `DERIVED_BY` names the law that
produces each so nothing derives itself.

**The constants are named as the CARD prints them.** `SYMBOLS` maps the equation
symbol to the calibration field behind it -- `c` is `sag_coeff`, `p` is
`sag_span_exp`, `Ea` is `activation_energy` -- and the shipped laws below are
written in those symbols. So the box seeds `c * span**p` under a card that reads
`depth = c span^p`, instead of seeding field names under mathematics and then
swapping one alphabet for the other the moment Apply is pressed. The long
spelling stays legal (`MEWModel._law_names` accepts both), so nothing that
already names `sag_coeff` breaks.

**The chain: `viscosity` -> `flow` -> `diameter`.** All three go through this
seam, so replacing the viscosity law moves the flow and the fiber diameter with
it. `eta` is passed INTO the flow law rather than recomputed there, so there is
no second place for the two to disagree.

**What is editable, and the one thing that is not.** The four laws here are plain
scalar evaluations -- something asks for a number and uses it. `lag_angle` is
NOT, and is deliberately absent: `integrate_contact_path_ray` does not call it,
it integrates the law's closed-form INVERSE --

    K = v_noz * ((amp - L_ss) / span) ** (1.0 / b)      mew_physics.py:1254

-- which exists only because the shipped form is linear in its saturating factor.
`mew_deposition.py:1350` and `mew_live.py:654` both feed that tracker from
`lag_ray_constants`. A user form outside `L0 + (A-L0)(1-eps^-b)` would leave the
exported STL following the tracker while the card displayed something else: a
SILENT divergence, which is the exact failure class this repo spends its guards
on. Making lag editable means replacing that closed form with a numerical
root-find inside a loop that runs `RAY_SUBSTEPS` times per sample -- a phase of
its own with a perf A/B, not a side effect of this one.

**This is a security boundary, not just a correctness one.** MEWracle Lite serves
the same engine over HTTP, so an expression can arrive from the network. There is
therefore no `eval` and no `compile` of user text anywhere below: `ast.parse`
gives a tree, `_compile` walks it against a whitelist and builds a tree of
closures, and anything not on the whitelist is refused by node type. No attribute
access, no subscripts, no calls except to the fixed function table, no names
except the ones the law declares, no literals except numbers.

**Cost.** A closure tree is several times slower than the native method, so a law
with an override is slower than one without -- which is why an empty expression
does not go through here at all: `MEWModel` calls the shipped method directly and
this module is never touched. The sag law is the one called per span.
"""

import ast
import math

MAX_SOURCE = 2000          # characters; a law is one line, not a program
MAX_DEPTH = 40             # nesting; ast.parse itself recurses


class LawError(ValueError):
    """A user expression that will not compile, or will not evaluate. The message
    is written to be shown in the GUI verbatim."""


# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------
def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


FUNCTIONS = {
    "exp": math.exp, "ln": math.log, "log": math.log, "log10": math.log10,
    "sqrt": math.sqrt, "abs": abs, "pow": pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "atan2": math.atan2, "sinh": math.sinh, "cosh": math.cosh,
    "tanh": math.tanh, "hypot": math.hypot,
    "floor": math.floor, "ceil": math.ceil,
    "radians": math.radians, "degrees": math.degrees,
    "min": min, "max": max, "clamp": _clamp,
}
# Every `temperature` in this language is degC -- that is what the GUI field, the
# worker argv and `ProcessParams` all carry. An Arrhenius law needs kelvin, so the
# offset is a NAMED constant: `temperature + T_abs` is checkable by eye, where a
# bare 273.15 in the middle of an exponent is a units bug waiting to happen.
CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "R": 8.314462618,        # molar gas constant, J/(mol*K)
    "T_abs": 273.15,         # degC -> K
}

_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b,
    ast.Mod: lambda a, b: a % b,
}
_CMPOPS = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
}


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------
def _compile(node, names, depth=0):
    """One AST node -> a closure `f(env) -> value`. Raises LawError on anything
    outside the whitelist, naming the construct so the GUI can say what it is."""
    if depth > MAX_DEPTH:
        raise LawError("expression nests too deeply")
    d = depth + 1

    if isinstance(node, ast.Expression):
        return _compile(node.body, names, d)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(
                node.value, (int, float)):
            raise LawError("only numbers can be written literally here")
        v = float(node.value)
        return lambda env: v

    if isinstance(node, ast.Name):
        nm = node.id
        if nm in CONSTANTS:
            v = CONSTANTS[nm]
            return lambda env: v
        if nm not in names:
            raise LawError(
                "unknown name %r -- this law can see: %s"
                % (nm, ", ".join(sorted(names))))
        def _name(env, nm=nm):
            try:
                return env[nm]
            except KeyError:
                raise LawError("%r has no value at this operating point" % nm)
        return _name

    if isinstance(node, ast.UnaryOp):
        inner = _compile(node.operand, names, d)
        if isinstance(node.op, ast.USub):
            return lambda env: -inner(env)
        if isinstance(node.op, ast.UAdd):
            return inner
        if isinstance(node.op, ast.Not):
            return lambda env: (not inner(env))
        raise LawError("unsupported unary operator")

    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise LawError(
                "unsupported operator -- use + - * / ** %% only")
        a = _compile(node.left, names, d)
        b = _compile(node.right, names, d)
        return lambda env: op(a(env), b(env))

    if isinstance(node, ast.Compare):
        left = _compile(node.left, names, d)
        parts = []
        for op, comp in zip(node.ops, node.comparators):
            fn = _CMPOPS.get(type(op))
            if fn is None:
                raise LawError("unsupported comparison")
            parts.append((fn, _compile(comp, names, d)))

        def _cmp(env):
            cur = left(env)
            for fn, nxt in parts:
                val = nxt(env)
                if not fn(cur, val):
                    return False
                cur = val
            return True
        return _cmp

    if isinstance(node, ast.BoolOp):
        vals = [_compile(v, names, d) for v in node.values]
        if isinstance(node.op, ast.And):
            return lambda env: all(v(env) for v in vals)
        return lambda env: any(v(env) for v in vals)

    if isinstance(node, ast.IfExp):
        # `a if cond else b` -- how a piecewise law is written
        test = _compile(node.test, names, d)
        yes = _compile(node.body, names, d)
        no = _compile(node.orelse, names, d)
        return lambda env: yes(env) if test(env) else no(env)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise LawError("only the built-in functions can be called")
        fn = FUNCTIONS.get(node.func.id)
        if fn is None:
            raise LawError(
                "unknown function %r -- available: %s"
                % (getattr(node.func, "id", "?"),
                   ", ".join(sorted(FUNCTIONS))))
        if node.keywords:
            raise LawError("functions here take positional arguments only")
        args = [_compile(a, names, d) for a in node.args]
        return lambda env: fn(*[a(env) for a in args])

    raise LawError(
        "%s is not allowed in a law expression" % type(node).__name__)


# ---------------------------------------------------------------------------
# the array tree -- the same whitelist, evaluated over numpy arrays
# ---------------------------------------------------------------------------
# The exact sag solver asks for a whole array of span depths in one call
# (`gcode_to_fibers.py`, `parab`). The shipped law broadcasts because it is
# plain arithmetic. Evaluating a user law span by span instead doubled a
# default-scaffold solve (17 s -> 37 s, ~2.5 million spans), so a user law gets
# a second closure tree that broadcasts too. It walks the SAME validated AST:
# `_compile` has already refused everything off the whitelist before this runs,
# so this tree adds no new way in. numpy is imported only when an array
# actually arrives.
#
# **The contract is exact: a batch answers what the spans answer one at a
# time.** Same values, and it raises whenever any one span would, with that
# span's error. The first version only checked the final value, so
# `exp(-1/span)` returned 0 at span = 0 where the per-span call refused it.
# Three rules make the fast path agree, and a fallback covers the rest:
#
#   * a numpy float error RAISES (`errstate`), as Python's float and `math` do;
#   * `a if c else b`, `and`/`or` and a comparison chain evaluate each operand
#     only on the elements that reach it, as Python's short-circuit does;
#   * a bool is a NUMBER in arithmetic (True + True is 2, where numpy's bool
#     `+` is a logical or).
#
# Anything the fast path cannot vouch for -- a float error, a complex value
# (Python's `(-8)**(1/3)`), a wrong argument count, a bool or non-finite
# result -- re-runs the batch span by span through the scalar `run`, which is
# the authority. That costs speed only for a law that is already failing or
# leaning on those corners.
_N = "\x00n"          # env key: the element count. Not a legal law name.


class _Fallback(Exception):
    """The fast path cannot vouch for this batch: evaluate span by span."""


def _array_functions(np):
    from functools import reduce

    def arity(fn, lo, hi):
        # a numpy ufunc takes a spare positional as its OUTPUT array, so
        # `exp(x, y)` would silently write into y where `math.exp` refuses it
        def f(*a):
            if not lo <= len(a) <= hi:
                raise _Fallback()
            return fn(*a)
        return f

    def _log(x, base=None):
        return np.log(x) if base is None else np.log(x) / np.log(base)

    def _hypot(*a):
        if not a:
            return 0.0
        return np.abs(a[0]) if len(a) == 1 else reduce(np.hypot, a)

    def _clamp_a(x, lo, hi):
        # the scalar `_clamp`'s order of tests, so lo > hi agrees too
        return np.where(x < lo, lo, np.where(x > hi, hi, x))

    one = lambda fn: arity(fn, 1, 1)                     # noqa: E731
    table = {
        "exp": one(np.exp), "ln": arity(_log, 1, 2), "log": arity(_log, 1, 2),
        "log10": one(np.log10), "sqrt": one(np.sqrt), "abs": one(np.abs),
        "pow": arity(np.power, 2, 2),
        "sin": one(np.sin), "cos": one(np.cos), "tan": one(np.tan),
        "asin": one(np.arcsin), "acos": one(np.arccos), "atan": one(np.arctan),
        "atan2": arity(np.arctan2, 2, 2), "sinh": one(np.sinh),
        "cosh": one(np.cosh), "tanh": one(np.tanh), "hypot": _hypot,
        "floor": one(np.floor), "ceil": one(np.ceil),
        "radians": one(np.radians), "degrees": one(np.degrees),
        "min": arity(lambda *a: reduce(np.minimum, a), 2, 99),
        "max": arity(lambda *a: reduce(np.maximum, a), 2, 99),
        "clamp": arity(_clamp_a, 3, 3),
    }
    assert set(table) == set(FUNCTIONS), "array table drifted from FUNCTIONS"
    return table


def _sub(env, idx, np):
    """`env` restricted to the elements `idx`: what a short-circuited operand
    sees, so it is never evaluated on an element that does not reach it."""
    out = {k: (v[idx] if isinstance(v, np.ndarray) else v)
           for k, v in env.items()}
    out[_N] = len(idx)
    return out


def _compile_array(node, np, fns):
    """One ALREADY-VALIDATED AST node -> a closure over flat arrays of length
    `env[_N]`. A closure returns a scalar or such an array."""
    rec = lambda n: _compile_array(n, np, fns)          # noqa: E731
    is_arr = lambda v: isinstance(v, np.ndarray)         # noqa: E731

    def num(v):
        if is_arr(v):
            if v.dtype == bool:
                return v.astype(float)
            if v.dtype.kind == "c":
                raise _Fallback()
            return v
        if isinstance(v, (bool, np.bool_)):
            return float(v)             # np.cos(True) is float16, not float64
        if isinstance(v, complex):
            raise _Fallback()
        return v

    if isinstance(node, ast.Expression):
        return rec(node.body)
    if isinstance(node, (ast.Constant, ast.Name)):
        return _compile(node, _AnyName())
    if isinstance(node, ast.UnaryOp):
        inner = rec(node.operand)
        if isinstance(node.op, ast.USub):
            return lambda env: -num(inner(env))
        if isinstance(node.op, ast.UAdd):
            return inner
        return lambda env: np.logical_not(inner(env))
    if isinstance(node, ast.BinOp):
        op = _BINOPS[type(node.op)]
        a, b = rec(node.left), rec(node.right)
        return lambda env: op(num(a(env)), num(b(env)))
    if isinstance(node, ast.Compare):
        left = rec(node.left)
        parts = [(_CMPOPS[type(o)], rec(c))
                 for o, c in zip(node.ops, node.comparators)]

        def _cmp(env):
            n = env[_N]
            cur, res, idx = left(env), None, None
            for fn, nxt in parts:
                val = nxt(env if idx is None else _sub(env, idx, np))
                r = fn(cur, val)
                if is_arr(r):
                    if idx is None:
                        idx, res = np.arange(n), np.ones(n, dtype=bool)
                    r = np.broadcast_to(r, idx.shape)
                    res[idx[~r]] = False
                    idx = idx[r]
                    cur = val[r] if is_arr(val) else val
                    if not len(idx):
                        break
                elif not r:
                    if res is None:
                        return False
                    res[idx] = False
                    break
                else:
                    cur = val
            return True if res is None else res
        return _cmp
    if isinstance(node, ast.BoolOp):
        vals = [rec(v) for v in node.values]
        conj = isinstance(node.op, ast.And)

        def _bool(env):
            # `all(...)` / `any(...)`: stop at the first operand that decides
            n = env[_N]
            res, idx = None, None
            for v in vals:
                x = v(env if idx is None else _sub(env, idx, np))
                if is_arr(x):
                    if idx is None:
                        idx, res = np.arange(n), np.full(n, conj)
                    decided = np.broadcast_to(x.astype(bool), idx.shape) != conj
                    res[idx[decided]] = not conj
                    idx = idx[~decided]
                    if not len(idx):
                        break
                elif bool(x) != conj:
                    if res is None:
                        return not conj
                    res[idx] = not conj
                    break
            return conj if res is None else res
        return _bool
    if isinstance(node, ast.IfExp):
        test, yes, no = rec(node.test), rec(node.body), rec(node.orelse)

        def _if(env):
            t = test(env)
            if not is_arr(t):
                return yes(env) if t else no(env)
            mask = t.astype(bool)
            if mask.all():
                return yes(env)
            if not mask.any():
                return no(env)
            iy, ino = np.flatnonzero(mask), np.flatnonzero(~mask)
            y = yes(_sub(env, iy, np))
            o = no(_sub(env, ino, np))
            kinds = {bool if (isinstance(v, (bool, np.bool_))
                              or (is_arr(v) and v.dtype == bool)) else float
                     for v in (y, o)}
            if len(kinds) != 1:
                raise _Fallback()       # a bool on some elements only
            kind = kinds.pop()
            out = np.empty(env[_N], dtype=kind)
            out[iy] = y if kind is bool else num(y)
            out[ino] = o if kind is bool else num(o)
            return out
        return _if
    if isinstance(node, ast.Call):
        fn = fns[node.func.id]
        args = [rec(a) for a in node.args]
        if node.func.id not in ("min", "max", "clamp"):
            return lambda env: fn(*[num(a(env)) for a in args])

        # these return one of their ARGUMENTS, so Python's `min(False, 1)` is
        # the bool False -- a result `run` refuses. Casting it to 0.0 would
        # answer where the span on its own does not.
        def _pick(env):
            vals = [a(env) for a in args]
            if any(isinstance(v, (bool, np.bool_))
                   or (is_arr(v) and v.dtype == bool) for v in vals):
                raise _Fallback()
            return fn(*[num(v) for v in vals])
        return _pick
    raise LawError(                     # unreachable: `_compile` refused it
        "%s is not allowed in a law expression" % type(node).__name__)


class _AnyName(frozenset):
    """Every name is legal: used only to rebuild Constant/Name closures for a
    tree `_compile` has already checked against the law's real name set."""
    def __contains__(self, item):
        return True


def compile_law(source, names):
    """`source` (text) + `names` (the variables this law may see) -> `f(env)`.

    `env` is a dict of name -> number; `f` returns a float. Raises `LawError` for
    anything that will not compile, with a message meant for the user's eyes."""
    if not isinstance(source, str):
        raise LawError("a law must be text")
    src = source.strip()
    if not src:
        raise LawError("empty expression")
    if len(src) > MAX_SOURCE:
        raise LawError("expression is too long (%d characters, limit %d)"
                       % (len(src), MAX_SOURCE))
    # a law is ONE expression: `mode="eval"` is what refuses statements,
    # assignments, imports and semicolon-chained anything at the parser.
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise LawError("cannot read that expression: %s" % (exc.msg,))
    except (ValueError, MemoryError, RecursionError) as exc:
        raise LawError("cannot read that expression (%s)" % type(exc).__name__)
    fn = _compile(tree, set(names))

    def run(env):
        try:
            val = fn(env)
        except LawError:
            raise
        except ZeroDivisionError:
            raise LawError("division by zero at this operating point")
        except (ValueError, OverflowError) as exc:
            raise LawError("undefined at this operating point: %s" % (exc,))
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise LawError("the expression must produce a number")
        if val != val or val in (float("inf"), float("-inf")):
            raise LawError("the expression produced a non-finite value")
        return float(val)

    array_fn = []

    def run_array(env):
        """`run` for an env holding numpy arrays: a float array of the broadcast
        shape, equal element for element to calling `run` on each span, and
        raising exactly when one of those calls would (see the array tree)."""
        import numpy as np
        if not array_fn:
            array_fn.append(_compile_array(tree, np, _array_functions(np)))
        arrays = [k for k, v in env.items() if isinstance(v, np.ndarray)]
        shape = np.broadcast_shapes(*(env[k].shape for k in arrays))
        n = 1
        for s in shape:
            n *= s
        flat = dict(env)
        for k in arrays:
            flat[k] = np.ascontiguousarray(
                np.broadcast_to(env[k], shape), dtype=float).reshape(-1)
        flat[_N] = n
        try:
            with np.errstate(divide="raise", over="raise", invalid="raise",
                             under="ignore"):
                val = np.asarray(array_fn[0](flat))
                if val.dtype.kind not in "iuf" or not np.all(np.isfinite(val)):
                    raise _Fallback()
                return np.array(np.broadcast_to(val, (n,)),
                                dtype=float).reshape(shape)
        except Exception:               # noqa: BLE001 -- the scalar path decides
            pass
        # span by span through `run`: its value, or the error of the first
        # span that has one
        run_array.fallbacks += 1
        del flat[_N]
        out = np.empty(n)
        for i in range(n):
            e = dict(flat)
            for k in arrays:
                e[k] = float(flat[k][i])
            out[i] = run(e)
        return out.reshape(shape)

    run_array.fallbacks = 0     # how many batches went span by span
    run.array = run_array
    run.source = src
    # what the expression actually MENTIONS. `_law_scope` derives a quantity only
    # when the law names it, so the sag law -- called once per span -- pays
    # nothing for the four derived quantities it does not use.
    run.uses = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return run


def names_in(source):
    """Every bare name `source` mentions, whether or not it is legal. For the
    editor's 'you used X, which this law cannot see' hint -- never for a
    permission decision, which `compile_law` alone makes."""
    try:
        tree = ast.parse(str(source or ""), mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


# ---------------------------------------------------------------------------
# the ACTIVE form: the shipped law minus the terms this calibration turns off
# ---------------------------------------------------------------------------
# Every optional hook in this build is written `... if <exp> != 0 else 1`, and
# every one of those exponents ships at 0. Seeding the editor with the raw
# `shipped` string therefore asks the user to read around four guard clauses
# that are all taking the `else 1` branch -- the sag law reads as a four-factor
# product when what is running is `c * span**p`, and
# CTS reads as five factors when what is running is the bare `cts_ref`. That gap
# between the typeset law on the card and the text in the box is what made the
# first version of this editor unusable.
#
# So the editor seeds from the form that is ACTUALLY running at the user's own
# calibration. It is FOLDED, not a second hand-written string: a hand-written
# "simple" form would be a third copy to keep in step with `mew_physics`, and it
# would be wrong the moment somebody fits a hook. Folding cannot drift -- fit
# `sag_diam_exp` and the diameter term reappears on its own, because its guard
# now takes the other branch.
#
# Only two rewrites are made, and both mean "this term is exactly 1 for every
# possible input", never "this term is small":
#
#   * `a if C else b` where C is decidable from calibration constants alone
#   * `x ** n` where n is a constant that is exactly 0
#
# `x ** 1` is deliberately NOT folded. It is 1 only in the exponent, not in the
# value, and folding it would delete the name of a coefficient the user can see
# and edit on the card above.
def _const(node, consts):
    """`(True, value)` when `node` is decidable from the calibration constants
    alone -- no law argument, no process context, no user coefficient. Anything
    that will not compile against that name set, or raises while evaluating,
    is simply not constant: `(False, None)`."""
    try:
        return True, _compile(node, set(consts))(consts)
    except (LawError, ArithmeticError, TypeError, ValueError, KeyError):
        return False, None


def _is_one(node):
    return (isinstance(node, ast.Constant)
            and not isinstance(node.value, bool)
            and isinstance(node.value, (int, float))
            and float(node.value) == 1.0)


def _fold(node, consts):
    """Rewrite `node` bottom-up under the two rules above. Returns the node to
    use in its place (possibly the same one)."""
    for field, old in ast.iter_fields(node):
        if isinstance(old, ast.AST):
            setattr(node, field, _fold(old, consts))
        elif isinstance(old, list):
            setattr(node, field, [_fold(v, consts) if isinstance(v, ast.AST)
                                  else v for v in old])

    if isinstance(node, ast.IfExp):
        ok, val = _const(node.test, consts)
        if ok:
            return node.body if val else node.orelse
        return node

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Pow):
            ok, val = _const(node.right, consts)
            if ok and val == 0:
                return ast.Constant(value=1)
        elif isinstance(node.op, ast.Mult):
            if _is_one(node.right):
                return node.left
            if _is_one(node.left):
                return node.right
    return node


def active_source(key, constants):
    """The law `key` as it is actually evaluated at `constants` -- the shipped
    form with every off hook folded away. `constants` is `MEWModel._law_constants()`.

    Falls back to the shipped string rather than raising: this feeds a text box,
    and a fold that cannot be done is a cosmetic loss, not an error."""
    spec = LAWS.get(key)
    if spec is None:
        raise LawError("%r is not an editable law" % (key,))
    try:
        tree = ast.parse(spec.shipped, mode="eval")
        folded = ast.Expression(body=_fold(tree.body, dict(constants or {})))
        return ast.unparse(ast.fix_missing_locations(folded))
    except (SyntaxError, ValueError, TypeError, AttributeError,
            RecursionError, MemoryError):                 # pragma: no cover
        return spec.shipped


def normalize(src):
    """`src` reduced to its SHAPE -- parsed and unparsed, so spacing, redundant
    brackets and line breaks stop counting as an edit. Unparseable text comes
    back stripped, which can only fail to match, never falsely match."""
    # .strip() FIRST: `mode="eval"` treats a leading space as an indent and
    # raises, so an un-stripped box would silently fall to the raw-text branch
    # and read as an edit. `compile_law` strips for the same reason.
    try:
        return ast.unparse(ast.parse(str(src or "").strip(), mode="eval"))
    except (SyntaxError, ValueError, MemoryError, RecursionError,
            AttributeError):
        return str(src or "").strip()


def is_shipped(key, source, constants=None):
    """True when `source` is the shipped law in ANY of its spellings.

    The editor seeds from the folded form, so a user who changed nothing hands
    back text that is not character-identical to `shipped`; storing THAT as an
    override would freeze the folded-away hooks out of the model for good. Both
    spellings therefore have to read as "no override": `shipped` itself, guards
    and all, and the form folded against the calibration passed in.

    Comparison is on the PARSED shape, so re-indenting the box or dropping a
    space around a `*` is not an edit.

    **What this CANNOT decide**, and where the other half of the answer lives:
    `constants` comes back empty whenever the rig fields will not parse -- a
    half-typed entry is enough -- and the folded form then matches neither
    spelling, because folding against nothing just gives `shipped` back. An
    untouched Apply in that state used to store an override and rewrite the
    card's equation in front of the user. `gui_lawedit.apply` closes that by
    comparing against the text the panel actually seeded, which needs no
    calibration at all; this function is the second opinion, not the first."""
    src = normalize(source)
    spec = LAWS.get(key)
    if spec is None or not src:
        return not src
    return (src == normalize(spec.shipped)
            or src == normalize(active_source(key, constants or {})))


# ---------------------------------------------------------------------------
# which laws are editable, and what each one can see
# ---------------------------------------------------------------------------
class LawSpec:
    """One editable law: where it plugs in, what its expression may name, and the
    SHIPPED form written in this language.

    `shipped` is not decoration. Evaluated against the Python method over a
    parameter sweep, it keeps the registry from drifting away from
    `mew_physics` silently -- the comparator is the method itself, which
    this module did not write. It is also the source `active_source` folds.

    The editor does NOT seed from it directly. `shipped` carries every optional
    hook's guard, and every one of those exponents ships at 0, so the raw string
    reads as a product of dead clauses: a user who came to add `+ C` to the sag
    law met a four-factor expression where the card above said `depth = c span^p`.
    `active_source(key, constants)` is what the box gets."""

    __slots__ = ("key", "title", "args", "result", "shipped", "note", "symbol")

    def __init__(self, key, title, args, result, shipped, note="", symbol=""):
        self.key = key            # stable id; the profile key and the GUI's handle
        self.title = title        # the module card it belongs to
        self.args = args          # ((name, "what it is"), ...) -- the law's inputs
        self.result = result      # what the number means, with its unit
        self.shipped = shipped    # the shipped law in this language
        self.note = note
        # what the law PRODUCES, in the same spelling the card typesets it. Two
        # laws share the Material card, so their panels have to say which is
        # which; a card with one law does not repeat itself.
        self.symbol = symbol


LAWS = {
    # The two laws the fiber-diameter chain is BUILT on. Both were "inputs" when
    # this registry was first written, on the reading that the Material card is
    # where a law's constants live rather than where a law lives. That was wrong
    # twice over: the card typesets both of them as equations, and the substitution
    # a MEW user is most likely to want -- a Cross or Carreau melt viscosity in
    # place of Arrhenius, because their polymer shear-thins at the wall -- is
    # exactly a change of FORM that no exponent hook can express.
    #
    # `viscosity` feeds `flow`, `flow` feeds `diameter`, and each is evaluated
    # through this seam, so overriding the first one changes all three. That is the
    # intent: eta is one quantity with one name, whichever law produced it.
    #
    # `title` is the CARD each one is edited on, and `flow` says Fiber diameter
    # rather than Material and machine parameters: the constants it names (r, L)
    # are on the material card, but the chain that sets the fibre thickness is
    # Q-then-d, and splitting it across two cards sent a reader asking "what
    # sets my fibre thickness" to the wrong page for half the answer.
    "viscosity": LawSpec(
        "viscosity", "Material and printer parameters",
        (("temperature", "print temperature, degC"),),
        "melt viscosity at that temperature, Pa.s",
        "eta_ref * exp(Ea / R"
        " * (1/(temperature + T_abs) - 1/(T_ref + T_abs)))",
        "Arrhenius, the standard form for a polymer melt well above its Tm, "
        "as PCL is at MEW temperatures. Ea alone sets how the model responds "
        "to temperature, because the viscosity_ref prefactor cancels out of "
        "d ln(eta)/dT. A shear-thinning form (Cross, Carreau) is what this box "
        "is for. The wall shear rate here is about 0.1-0.3 1/s, deep in PCL's "
        "Newtonian plateau, so a shear-rate term is a claim about your own "
        "polymer.",
        symbol="eta"),
    "flow": LawSpec(
        "flow", "Fiber diameter",
        (("pressure", "applied pressure, Pa"),
         ("temperature", "print temperature, degC"),
         ("eta", "melt viscosity at that temperature, Pa.s -- already through "
                 "the viscosity law above")),
        "volumetric melt flow, mm^3/s",
        "(pi * r**4 * pressure / (8 * eta * L)"
        "   if pressure > 0 else 0)",
        "Hagen-Poiseuille through the needle bore. It needs no unit factors: "
        "with the bore in mm, pressure in Pa and eta in Pa.s, r^4/L comes out "
        "in mm^3/s. Flow data can only fit the group "
        "pi*r^4/(8*viscosity_ref*L), so r, L and viscosity_ref trade off "
        "against each other: fit them together or not at all. Pressure at or "
        "below ambient gives no flow.",
        symbol="Q"),
    "diameter": LawSpec(
        "diameter", "Fiber diameter",
        (("Q", "volumetric flow rate, mm^3/s"),
         ("speed", "print speed, mm/s")),
        "deposited fiber diameter, mm",
        "sqrt(4*Q / (pi*speed))",
        "Mass conservation, and the one law here with no constants of its own. "
        "Q arrives from the flow law printed under it on this same card, and "
        "eta from the viscosity law on Material and printer parameters, so a "
        "pressure or temperature dependence belongs in one of those rather "
        "than here.",
        symbol="d"),
    "cts": LawSpec(
        "cts", "Critical translation speed",
        (("voltage", "applied voltage, kV"),
         ("gap", "nozzle-collector gap (working distance), mm"),
         ("Q", "volumetric flow rate, mm^3/s"),
         ("eta", "melt viscosity at the print temperature, Pa.s")),
        "critical translation speed, mm/s",
        "CTS_ref"
        " * (voltage/cts_voltage_ref)**cts_voltage_exp"
        " * (gap/cts_gap_ref)**cts_gap_exp"
        " * ((Q/cts_flow_ref)**cts_flow_exp if cts_flow_exp != 0 else 1)"
        " * ((eta/cts_visc_ref)**cts_visc_exp if cts_visc_exp != 0 else 1)",
        "The default exponents are all 0 on purpose: CTS is a balance of draw "
        "and drag, so flow and temperature set the fiber diameter, not the "
        "landing speed. Anything you write here is a claim about your printer, "
        "not about MEW.",
        symbol="CTS"),
    "sag": LawSpec(
        "sag", "Sagging",
        (("span", "unsupported span between the two crossings, mm"),
         ("d", "fiber diameter, mm"),
         ("visc_ratio", "eta(temperature)/eta(T_ref), 1 when unused")),
        "free midspan sag, mm",
        "c * span**p"
        " * ((d/sag_diam_ref)**sag_diam_exp"
        "    if sag_diam_exp != 0 and sag_diam_ref > 0 else 1)"
        " * (visc_ratio**sag_thermal_visc_exp"
        "    if sag_thermal_visc_exp != 0 else 1)",
        "The span exponent 2 comes from the viscous catenary, not from a fit. "
        "The solver runs this law once per span, so on a large scaffold an "
        "expression here costs noticeable time.",
        symbol="depth"),
    "fusion": LawSpec(
        "fusion", "Fusion",
        (("phi", "the calibrated overlap fraction for this contact kind"),
         ("d", "fiber diameter, mm"),
         ("visc_ratio", "eta(temperature)/eta(T_ref), 1 when unused")),
        "centre-sink depth at one contact, mm",
        "min(max(phi"
        "        * ((d/fusion_diam_ref)**fusion_diam_exp"
        "           if fusion_diam_exp != 0 and fusion_diam_ref > 0 else 1)"
        "        * (visc_ratio**fusion_thermal_visc_exp"
        "           if fusion_thermal_visc_exp != 0 else 1),"
        "        0), phi_max) * d",
        "phi already has the right size for the contact, a junction or a "
        "wall, so one expression covers both, as the default law does. Keep "
        "the clamp unless you mean upper fibers to reach the lower fiber's "
        "center.",
        symbol="sink"),
    # The MANDREL's circumferential lag, the paper's Eq. 7. Editable where `lag` is
    # not, because nothing integrates it: `ContactPlacement.circ_radius` asks
    # for one number per lag and hands it to Eq. 4's contact solve, so a typed
    # form reaches the STL through the same call the shipped one does
    # (`MEWModel.circumferential_lag`). Four-axis only -- a five-axis patch has
    # two curvatures and no single `r_m`, so `CurvedPlacement` keeps its own.
    # `r_m`, not the paper's `r`: `r` is already the needle bore on the Fiber
    # diameter card, and one letter meaning two radii in one vocabulary is the
    # confusion `QUANTITIES` exists to prevent.
    "rcir": LawSpec(
        "rcir", "Jet lag",
        (("R_ax", ""), ("theta", ""), ("r_m", ""), ("gap", "")),
        "circumferential lag radius on the mandrel, mm of arc",
        "r_m * (theta - asin(r_m * sin(theta) / (r_m + gap)))",
        "The jet is assumed to lean by the same angle theta it has on a flat "
        "collector, measured from the mandrel's surface normal at the contact "
        "point; the triangle formed by the axis, the nozzle and the contact "
        "gives this arc. It tends to R_ax as r_m grows, and never passes the "
        "tangent point. Against Neuhaus et al.'s fitted radii it runs 6-27% "
        "low. A measured R_ax/R_cir pair from a calibration tube still "
        "overrides whatever is written here, and the tangency bound is applied "
        "after it.",
        symbol="R_cir"),
}

# ONE NAME PER QUANTITY, across every law and the process context, AND the same
# name the card above the box prints. `speed` is never `v`, `voltage` is never
# `V`, and the absolute viscosity `eta` (Pa.s) is never spelled the same as the
# dimensionless ratio `visc_ratio`. A law's argument may share a name with a
# process value ONLY when it is the same quantity in the same unit --
# `_law_scope` puts arguments last, so the argument wins and the two readings
# agree by construction.
#
# EVERY quantity below is usable in EVERY law. That is the whole point of one
# vocabulary: a user reading the Fiber-diameter card should not have to find out
# by trial that `eta` is a name the Material card knows and this one does not.
# The five that are not any law's argument at its own call site are DERIVED --
# `MEWModel._law_scope` computes them from the process context on demand, and
# `DERIVED_BY` says which law owns each, so a law never derives itself.
# The description carries the SYMBOL the cards print, because the language
# spells these in words and the equations do not: `speed` is drawn `v`, `gap` is
# drawn `H`, `pressure` is drawn `dP`. Renaming the quantities to single letters
# was the other way out and is the one this build already rejected -- `v` and
# `V` one keystroke apart, in a box where a typo is a silently different law.
QUANTITIES = {
    "Q": "volumetric melt flow rate Q, mm^3/s",
    "d": "deposited fiber diameter d, mm",
    "eta": "melt viscosity eta at the print temperature, Pa.s",
    "visc_ratio": "eta(T) / eta(T_ref), dimensionless",
    "CTS": "critical translation speed CTS, mm/s",
    "speed": "print speed v, mm/s",
    "voltage": "applied voltage V, kV",
    "pressure": "applied pressure dP, Pa",
    "temperature": "print temperature T, degC",
    "gap": "nozzle-collector gap H (working distance), mm",
    "span": "unsupported span between two crossings, mm -- sagging only",
    "phi": "overlap fraction phi for this contact -- fusion only",
    "R_ax": "axial lag R_ax, mm -- mandrel R_cir only",
    "theta": "lean angle, rad, = atan(R_ax/H) -- R_cir only",
    "r_m": "mandrel radius r_m, mm -- mandrel R_cir only",
}

# ONE copy of each description, and the registry takes it from here rather than
# keeping its own in step by hand. `LawSpec.args` carrying its own wording is
# exactly how one quantity comes to be described two ways on two cards; this is
# the structural answer, not another hand-synchronised copy.
for _spec in LAWS.values():
    _spec.args = tuple((a, QUANTITIES.get(a, d)) for a, d in _spec.args)
del _spec

# A derived quantity and every law that would have to RUN to produce it. Named
# inside any of them it is a cycle -- `d` needs the diameter law, which needs the
# flow law, which needs the viscosity law -- so `_law_scope` leaves it out there
# and the user gets "no value at this operating point", which is the truth. The
# runtime also carries a `_deriving` guard for the cycles a USER law can invent
# that this table cannot know about; this table is what keeps the ordinary case
# from paying for the guard.
DERIVED_BY = {
    "eta": ("viscosity",),
    "visc_ratio": ("viscosity",),
    "Q": ("flow", "viscosity"),
    "d": ("diameter", "flow", "viscosity"),
    "CTS": ("cts", "diameter", "flow", "viscosity"),
}

# Process values a law may name even though they are not its own arguments. This
# is what makes "give my law a voltage dependence" possible at all: they are
# constant within a print, so `MEWModel.context` carries them from whoever set
# the process up (the GUI, or a worker's argv) down to the law.
# The wording is TAKEN from QUANTITIES rather than repeated here. Two copies of
# "print speed, mm/s" is exactly how `v` and `speed` came to mean the same thing
# in two places, and a law editor that describes the same name two ways on two
# cards is the confusion this whole naming pass exists to remove.
CONTEXT_NAMES = {n: QUANTITIES[n] for n in
                 ("voltage", "pressure", "temperature", "gap", "speed")}

# ---------------------------------------------------------------------------
# the calibration constants, under the symbol the CARD prints
# ---------------------------------------------------------------------------
# The shipped laws above are written in these symbols, so the text the editor
# seeds into the box is the same alphabet as the equation typeset above it:
# `c * span**p` under `depth = c span^p`, not `sag_coeff * span**sag_span_exp`
# under it. That mismatch is the whole reason the first version of this editor
# read as "Apply mangled my law" -- the box spoke field names and the card spoke
# mathematics, and pressing Apply swapped one for the other in front of the user.
#
# ONLY the constants a card actually typesets get a symbol. Inventing one for
# `sag_diam_exp` would be notation nobody has ever written down; the unfitted
# hooks keep their field name, which is also what the Physics page labels them,
# and they only appear in the box at all once their exponent is fitted.
#
# The field name stays legal everywhere (`MEWModel._law_names` accepts both), so
# a law typed against the long spelling keeps working.
SYMBOLS = {
    "c": "sag_coeff",
    "p": "sag_span_exp",
    "eta_ref": "viscosity_ref",
    "Ea": "activation_energy",
    "T_ref": "temperature_ref",
    "r": "inner_radius",
    "L": "length",
    "phi_junction": "fusion_overlap",
    "phi_wall": "fusion_wall_overlap",
    "phi_max": "fusion_max_overlap",
    "CTS_ref": "cts_ref",
    # the jet-lag card. Its law is NOT editable (see above) but it IS typeset on
    # the page, so its constants have to answer to the letters it prints or the
    # card and the reference table describe the same rig in two alphabets.
    # `A` IS the stored field: it was a derived `tan(theta_ceiling)` sitting under
    # a separate per-rig multiplier `k`, which was the same freedom written twice.
    "A": "lag_amplitude",
    "b": "lag_saturation_exp",
    "theta_CTS": "lag_angle_cts_deg",
}
ATTR_SYMBOL = {attr: sym for sym, attr in SYMBOLS.items()}

# Constants the jet-lag card prints, in the order it prints them. Listed by hand
# because `lag` is not in `LAWS` -- there is no `shipped` string to read them out
# of -- so keep every one in step with `Calibration` by hand.
LAG_CONSTANTS = ("A", "b", "theta_CTS")

# Constants a law names that have NO `FieldSpec` behind them, so the reference
# table cannot take a description off the Physics page. Empty today -- the one
# entry it held, `sag_diam_ref`, is a dormant-hook constant and no longer
# reaches the table at all (see `DORMANT_HOOKS`). Kept because the mechanism is
# what stops the next such name printing a bare "calibration constant".
NO_FIELD_DESC = {}

# The unfitted hooks: every constant that belongs to a term the shipped law
# guards with `if <exponent> != 0`, where that exponent ships at 0. Each one is
# a `<term>_exp` and, where the term has a pivot, its `<term>_ref`.
#
# They are in the law strings, so `constant_names` finds them -- but the term
# they belong to is multiplied out to exactly 1 in every shipped calibration,
# and the editor seeds the box with the FOLDED law, in which they do not appear.
# So the reference table was printing thirteen names that are in no equation the
# user can see, next to a "Value now" column of 0s and inert pivots. That is the
# `contact_damping_zeta` mistake again: a number that looks worth typing because
# the table offered it.
#
# They stay legal NAMES -- `MEWModel._law_names` takes every `Calibration`
# field, so a law written against one still compiles, and fitting an exponent on
# the card still brings its term back into the seeded text. The table is a menu
# of what the page's equations use, not an index of the dataclass.
DORMANT_HOOKS = frozenset({
    "cts_flow_exp", "cts_flow_ref",
    "cts_gap_exp", "cts_gap_ref",
    "cts_visc_exp", "cts_visc_ref",
    "cts_voltage_exp", "cts_voltage_ref",
    "sag_diam_exp", "sag_diam_ref",
    "sag_thermal_visc_exp",
    "fusion_diam_exp", "fusion_diam_ref",
    "fusion_thermal_visc_exp",
})


def quantity_rows():
    """The live quantities, in reading order: `(name, what it is)`.

    One list, handed to every card unchanged. `LawSpec.args` is what a law is
    HANDED; this is what it may NAME, which is the question the reference panel
    is actually answering."""
    return [(n, QUANTITIES[n]) for n in QUANTITIES]


def constant_names():
    """Every constant the Physics page's EQUATIONS name, as a set of the spelling
    the table should print -- the symbol where a card prints one, the field name
    otherwise.

    Read out of the shipped law strings rather than listed, so it cannot go
    stale: add a term to a law and its constant appears here on its own. The
    jet-lag four are appended because that card's law is typeset but not
    editable, so it has no `shipped` string to read.

    What this deliberately EXCLUDES is every calibration field no equation
    mentions -- `stable_window` (a regime LABEL in the readout, not a term),
    `contact_damping_zeta` (retired: `integrate_contact_path_ray` has no damping
    term and reaches no geometry), `density`, `field_tip_strength`, the coil and
    cone render constants, `lag_amplitude_gap` (a note on where A was fitted),
    and the dormant `sag_creep_*` family. They are all still legal NAMES --
    `MEWModel._law_names` takes every field -- but a reference table that lists
    a number no law on this page uses is how `contact_damping_zeta` came to look
    like something worth typing.

    `DORMANT_HOOKS` goes out for the same reason one step further in: those ARE
    in the law strings, but only inside a term that folds to 1 at every shipped
    calibration, so they are in no equation the user can read on the page."""
    skip = set(FUNCTIONS) | set(CONSTANTS) | set(QUANTITIES)
    out = set(LAG_CONSTANTS)
    for spec in LAWS.values():
        args = {a for a, _d in spec.args}
        out |= (names_in(spec.shipped) - skip - args)
    out |= {"phi_junction", "phi_wall"}     # the two magnitudes behind `phi`
    out -= DORMANT_HOOKS
    return out
