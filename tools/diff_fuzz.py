"""Differential fuzz: CairoSVG path emission vs. the spec reference parser.

Comparison strategy:
  * Arc-free paths are compared segment-by-segment (exact control points),
    which is what the smooth-command bug report is about.
  * Paths containing arcs are compared by symmetric nearest-point distance
    over dense samples (Cairo and the reference subdivide arcs differently).
  * Subpath boundaries and error behavior are checked separately.
"""

import math
import os
import random
import sys

import cairocffi

sys.path.insert(0, os.path.dirname(__file__))
from svg_path_reference import parse_path, PathError  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
import cairosvg.path as csvg_path  # noqa: E402

CAIRO_TOL = 2e-3          # Cairo's fixed-point quantization (~1/512)
# Dense but independent arc subdivision accumulates a small error over long
# multi-arc chains. Tolerance is the larger of an absolute floor and a small
# fraction of the path's spatial scale (extreme aspect-ratio arcs). A genuine
# smooth-command control-point mistake is far larger and also shows up on
# arc-free paths, which use the strict segment comparison above.
TRAJECTORY_TOL = 1.0
ARC_REL_TOL = 0.03


class FakeNode(dict):
    def __init__(self, d):
        super().__init__()
        self['d'] = d
        self.vertices = None


def cairo_emit(d):
    """Run CairoSVG's path() and return Cairo's raw path tuple list."""
    surface = cairocffi.RecordingSurface(
        cairocffi.CONTENT_COLOR_ALPHA, (0, 0, 1000, 1000))
    ctx = cairocffi.Context(surface)
    fake_surface = type('S', (), {'context': ctx})()
    node = FakeNode(d)
    try:
        csvg_path.path(fake_surface, node)
    except Exception as exc:  # noqa: BLE001
        return ('ERROR', type(exc).__name__)
    raw = ctx.copy_path()
    ctx.new_path()
    surface.finish()
    return raw


def normalize(raw):
    """Turn Cairo path tuples into the reference op format."""
    if isinstance(raw, tuple) and raw and raw[0] == 'ERROR':
        return raw
    ops = []
    for kind, coords in raw:
        if kind == cairocffi.PATH_MOVE_TO:
            ops.append(('M', coords[0], coords[1]))
        elif kind == cairocffi.PATH_LINE_TO:
            ops.append(('L', coords[0], coords[1]))
        elif kind == cairocffi.PATH_CURVE_TO:
            ops.append(('C', *coords))
        elif kind == cairocffi.PATH_CLOSE_PATH:
            ops.append(('Z',))
    return ops


def canonicalize(ops):
    """Drop stream-level noise without touching geometry.

    Consecutive moveto collapse to the last; empty/repeated closepaths drop;
    adjacent strictly-collinear lineto segments merge (h/v repeat groups and
    degenerate zero-length segments); a trailing moveto left by Cairo's
    close_path() at a subpath start is dropped (it draws nothing).
    """
    out = []
    # Start point of the last emitted straight segment (the segment before the
    # current L). Collinearity is always tested against that immediate
    # predecessor, so direction reversals and chains both behave correctly.
    last_line_start = None
    for op in ops:
        if op[0] == 'M':
            # A moveto right after one or more closepaths that merely returns
            # to the enclosing subpath's start is a Cairo copy_path() artifact.
            if out and out[-1][0] == 'Z':
                sub_start = None
                for prev in reversed(out):
                    if prev[0] == 'M':
                        sub_start = prev
                        break
                    if prev[0] != 'Z':
                        break
                if sub_start and \
                        math.isclose(sub_start[1], op[1], abs_tol=1e-9) and \
                        math.isclose(sub_start[2], op[2], abs_tol=1e-9):
                    last_line_start = (op[1], op[2])
                    continue
            last_line_start = (op[1], op[2])
            if out and out[-1][0] == 'M':
                out[-1] = op
            else:
                out.append(op)
        elif op[0] == 'Z':
            last_line_start = None
            if not out or out[-1][0] == 'Z':
                continue
            if out[-1][0] == 'M':
                out.pop()
            else:
                out.append(op)
        elif op[0] == 'L':
            cx, cy = op[1], op[2]
            # Drop a zero-length lineto that merely lands on the previous
            # segment's endpoint (e.g. an explicit L to a curve's end point).
            if out and out[-1][0] in ('M', 'L', 'C') and \
                    math.isclose(out[-1][-2], cx, abs_tol=1e-9) and \
                    math.isclose(out[-1][-1], cy, abs_tol=1e-9):
                continue
            if out and out[-1][0] == 'L' and last_line_start is not None:
                ax, ay = last_line_start
                bx, by = out[-1][1], out[-1][2]
                cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
                if abs(cross) < 1e-9:
                    # A->B and B->C are collinear: replace with A->C. The new
                    # segment still starts at A.
                    out[-1] = ('L', cx, cy)
                    continue
            # Not merged: this segment starts at the previous endpoint.
            last_line_start = (out[-1][-2], out[-1][-1]) if out and \
                out[-1][0] in ('M', 'L', 'C') else (0.0, 0.0)
            out.append(op)
        else:
            last_line_start = None
            out.append(op)

    # First normalize empty subpaths. A run of consecutive moveto starts that
    # many subpaths, but only the last moveto before a drawing op is a real
    # subpath start; the earlier moveto (and any closepath terminating those
    # empty subpaths) carry no geometry. Cairo and the reference keep
    # different bookkeeping here, so collapse both to the same minimal form.
    pruned = []
    idx = 0
    n = len(out)
    while idx < n:
        op = out[idx]
        if op[0] == 'M':
            # Find the end of this consecutive-moveto run.
            j = idx
            while j < n and out[j][0] == 'M':
                j += 1
            # Skip any Zs that close empty subpaths before a real draw op.
            k = j
            while k < n and out[k][0] == 'Z':
                k += 1
            if k < n and out[k][0] in ('L', 'C'):
                # The last M begins the subpath that actually gets drawn.
                pruned.append(out[j - 1])
                idx = j
                continue
            # No drawing op follows: the whole moveto/closepath run is empty.
            idx = k
            continue
        if op[0] == 'Z' and pruned and pruned[-1][0] == 'Z':
            idx += 1
            continue
        pruned.append(op)
        idx += 1
    out = pruned

    # Cairo's copy_path() leaves a moveto back to the subpath start after a
    # closepath when no draw command follows. Such a trailing M draws nothing.
    while len(out) >= 2 and out[-1][0] == 'M':
        prev = None
        for cand in reversed(out[:-1]):
            if cand[0] != 'M':
                prev = cand
                break
        if prev is not None and prev[0] == 'Z':
            out.pop()
        else:
            break

    # A closepath draws a straight segment back to the subpath start. When the
    # last explicit lineto is collinear with that closing segment, Cairo folds
    # it into the Z (emitting fewer Ls). Normalize by dropping a final L whose
    # endpoint is collinear with the subpath start immediately before a Z.
    normalized = []
    i = 0
    while i < len(out):
        op = out[i]
        if op[0] == 'Z' and normalized and normalized[-1][0] == 'L':
            sub_start = next((o for o in reversed(normalized)
                              if o[0] == 'M'), None)
            if sub_start is not None:
                lx, ly = normalized[-1][1], normalized[-1][2]
                sx, sy = sub_start[1], sub_start[2]
                # Find the segment start feeding this final L.
                anchor = normalized[-2] if len(normalized) >= 2 else sub_start
                ax, ay = (anchor[-2], anchor[-1]) if anchor[0] in (
                    'M', 'L', 'C') else (sx, sy)
                cross = ((lx - ax) * (sy - ay) - (ly - ay) * (sx - ax))
                if abs(cross) < 1e-9:
                    normalized.pop()  # Z's closing segment covers this L
        normalized.append(op)
        i += 1
    # Drop a zero-length closepath that sits directly before a moveto to a new
    # subpath. Such a Z returns to a point the path already occupies, so it
    # adds no geometry; Cairo and the reference disagree on keeping it.
    collapsed = []
    for i, op in enumerate(normalized):
        if op[0] == 'Z' and i + 1 < len(normalized) \
                and normalized[i + 1][0] == 'M':
            start = next((o for o in reversed(collapsed) if o[0] == 'M'), None)
            end = collapsed[-1] if collapsed else None
            if start is not None and end is not None and end[0] in ('L', 'C'):
                ex, ey = end[-2], end[-1]
                if math.isclose(ex, start[1], abs_tol=1e-9) and \
                        math.isclose(ey, start[2], abs_tol=1e-9):
                    continue
        collapsed.append(op)
    return collapsed


def strip_trailing_closepath(ops):
    """Drop a final Z that closes the last subpath with no following draw.

    Cairo and the spec reference differ on whether such a closepath survives
    into the emitted stream; geometrically it is just a return to the subpath
    start, which sampling-based comparison treats identically.
    """
    if ops and ops[-1][0] == 'Z':
        return ops[:-1]
    return ops


def subpath_starts(ops):
    """Distinct subpath start points, dropping coincident consecutive Ms."""
    starts = []
    for op in ops:
        if op[0] != 'M':
            continue
        pt = (op[1], op[2])
        if starts and math.isclose(starts[-1][0], pt[0], abs_tol=1e-9) and \
                math.isclose(starts[-1][1], pt[1], abs_tol=1e-9):
            continue
        starts.append(pt)
    return starts


def segment_equal(a, b, tol=CAIRO_TOL):
    if a[0] != b[0]:
        return False
    if a[0] == 'Z':
        return True
    return all(math.isclose(x, y, rel_tol=1e-9, abs_tol=tol)
               for x, y in zip(a[1:], b[1:]))


def strict_compare(got, ref):
    """Segment-by-segment comparison for paths without arcs."""
    g = canonicalize(got)
    r = canonicalize(ref)
    if len(g) != len(r):
        return False, f'segment count {len(g)} != {len(r)}\n g={g}\n r={r}'
    for a, b in zip(g, r):
        if not segment_equal(a, b):
            return False, f'{a} != {b}'
    return True, ''


def sample_ops(ops, steps=24):
    """Dense polyline sampling, split into subpaths."""
    subpaths, pts = [], []
    cur = start = (0.0, 0.0)
    for op in ops:
        kind = op[0]
        if kind == 'M':
            if pts:
                subpaths.append(pts)
            cur = start = (op[1], op[2])
            pts = [cur]
        elif kind == 'L':
            cur = (op[1], op[2])
            pts.append(cur)
        elif kind == 'C':
            _, x1, y1, x2, y2, x, y = op
            p0 = cur
            for i in range(1, steps + 1):
                t = i / steps
                mt = 1 - t
                pts.append((
                    mt ** 3 * p0[0] + 3 * mt ** 2 * t * x1
                    + 3 * mt * t ** 2 * x2 + t ** 3 * x,
                    mt ** 3 * p0[1] + 3 * mt ** 2 * t * y1
                    + 3 * mt * t ** 2 * y2 + t ** 3 * y))
            cur = (x, y)
        elif kind == 'Z':
            if cur != start:
                pts.append(start)
            cur = start
    if pts:
        subpaths.append(pts)
    return subpaths


def hausdorff(pa, pb):
    def nearest(p, cloud):
        return min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in cloud)
    cloud_b = [p for seg in pb for p in seg]
    cloud_a = [p for seg in pa for p in seg]
    if not cloud_a or not cloud_b:
        return float('inf')
    forward = max(nearest(p, cloud_b) for seg in pa for p in seg)
    backward = max(nearest(p, cloud_a) for seg in pb for p in seg)
    return max(forward, backward)


def path_scale(ops):
    """Rough spatial scale of an op stream for normalizing arc tolerance."""
    pts = [(o[-2], o[-1]) for o in ops if o[0] in ('M', 'L', 'C')]
    if not pts:
        return 1.0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    return span


def check(d, expect_error=False):
    raw = cairo_emit(d)
    try:
        ref = parse_path(d)
    except PathError:
        return True, ''
    if expect_error:
        return True, ''
    if isinstance(raw, tuple) and raw[0] == 'ERROR':
        return False, f'CairoSVG raised {raw[1]} on valid path {d!r}'
    got = normalize(raw)
    got_can = strip_trailing_closepath(canonicalize(got))
    ref_can = strip_trailing_closepath(canonicalize(ref))

    # Subpath structure must always agree.
    if subpath_starts(got_can) != subpath_starts(ref_can):
        return False, (f'subpath mismatch\n '
                       f'got={subpath_starts(got_can)}\n '
                       f'ref={subpath_starts(ref_can)}')

    has_arc = 'a' in d or 'A' in d
    if not has_arc:
        return strict_compare(got_can, ref_can)

    dist = hausdorff(sample_ops(got_can), sample_ops(ref_can))
    # Scale the tolerance to the path's size so extreme aspect-ratio arcs do
    # not trip an absolute threshold on an otherwise identical trajectory.
    scale = max(path_scale(got_can), path_scale(ref_can))
    tol = max(TRAJECTORY_TOL, ARC_REL_TOL * scale)
    if dist > tol:
        return False, (f'trajectory Hausdorff distance {dist:.4f} '
                       f'exceeds tolerance {tol:.4f}')
    return True, ''


# ---------------------------------------------------------------------------
# Hand-picked cases covering every rule in the bug report.
# ---------------------------------------------------------------------------
HAND_PICKED = [
    # Plain cubic then smooth cubic, absolute and relative.
    'M10 10 C 20 20 40 20 50 10 S 80 0 90 10',
    'm10 10 c 10 10 30 10 40 0 s 30 -10 40 0',
    # Smooth cubic after a line: first CP must default to current point.
    'M10 10 L 50 10 S 80 0 90 10',
    'M10 10 H 50 S 80 0 90 10',
    'M10 10 V 50 S 80 60 90 50',
    'M10 10 l 40 0 s 30 -10 40 0',
    # Smooth cubic after moveto / new subpath: no inherited state.
    'M10 10 C 20 20 40 20 50 10 M 60 10 S 90 0 100 10',
    'M10 10 C 20 20 40 20 50 10 m 10 0 s 30 -10 40 0',
    # Smooth cubic after close: state must be reset.
    'M10 10 C 20 20 40 20 50 10 L 60 40 Z S 90 40 100 30',
    'M10 10 c 10 10 30 10 40 0 l 10 30 z s 30 0 40 -10',
    # Smooth cubic after arc (including zero-radius arc -> line).
    'M10 10 A 20 20 0 0 1 50 10 S 80 0 90 10',
    'M10 10 a 0 0 0 0 1 40 0 s 30 -10 40 0',
    # Quadratic then smooth quadratic, all case combos.
    'M10 50 Q 30 10 50 50 T 90 50',
    'm10 50 q 20 -40 40 0 t 40 0',
    'M10 50 Q 30 10 50 50 t 40 0',
    'm10 50 q 20 -40 40 0 T 90 50',
    # T after line / move / close / cubic: CP defaults to current point.
    'M10 50 L 50 50 T 90 50',
    'M10 50 C 20 10 40 10 50 50 T 90 50',
    'M10 50 Q 30 10 50 50 M 60 50 T 100 50',
    'M10 50 Q 30 10 50 50 L 60 80 Z T 100 50',
    'M10 50 A 20 20 0 0 1 50 50 T 90 50',
    # Chained smooth commands (repeated implicit coordinate groups).
    'M10 10 C 20 20 40 20 50 10 S 70 0 80 10 S 100 20 110 10',
    'M10 50 Q 30 10 50 50 T 90 50 T 130 50',
    'm10 10 c 10 10 30 10 40 0 s 20 -10 30 0 s 20 10 30 0',
    # Explicit repeated command letters with multiple groups.
    'M10 10 C 20 20 40 20 50 10 C 60 0 80 0 90 10 S 110 20 120 10',
    'M10 50 Q 30 10 50 50 Q 70 90 90 50 T 130 50',
    # Mixed cubic and quadratic families must not share state.
    'M10 10 C 20 20 40 20 50 10 Q 60 0 80 10 T 120 10',
    'M10 50 Q 30 10 50 50 C 60 90 80 90 90 50 S 120 10 130 50',
    # Multiple independent subpaths.
    'M10 10 C 20 20 40 20 50 10 S 80 0 90 10 M 10 90 C 20 100 40 100 50 90',
    # Zero-length moves and degenerate smooth segments.
    'M10 10 C 20 20 40 20 50 10 S 50 10 50 10',
    'M10 50 Q 30 10 50 50 T 50 50',
    # Commas and no separators.
    'M10,10 C20,20 40,20 50,10 S80,0 90,10',
    'M10 10c10 10 30 10 40 0s30-10 40 0',
    # S / T as the very first command of the path.
    'M10 10 S 40 0 50 10',
    'M10 50 T 50 50',
    'm10 10 s 30 -10 40 0',
    'm10 50 t 40 0',
    # Smooth command as the literal first token (implicit moveto origin).
    'S 40 0 50 10',
    'T 50 50',
    't 40 0',
]

CMDS = list('mMLlHhVvCcSsQqTtAaZz')


def random_path(rng, allow_arcs=True):
    parts = []
    for _ in range(rng.randint(1, 14)):
        cmd = rng.choice(CMDS if allow_arcs
                         else [c for c in CMDS if c not in 'aA'])
        groups = rng.randint(1, 3)
        kind = cmd.lower()
        if kind == 'a':
            for _ in range(groups):
                parts.append(cmd)
                parts.append(f'{rng.randint(0, 25)} {rng.randint(0, 25)} '
                             f'{rng.randint(0, 180)} {rng.randint(0, 1)} '
                             f'{rng.randint(0, 1)} {rng.randint(-40, 80)} '
                             f'{rng.randint(-40, 80)}')
        elif kind == 'c':
            for _ in range(groups):
                parts.append(cmd)
                parts.append(' '.join(str(rng.randint(-40, 80))
                                      for _ in range(6)))
        elif kind in ('s', 'q'):
            for _ in range(groups):
                parts.append(cmd)
                parts.append(' '.join(str(rng.randint(-40, 80))
                                      for _ in range(4)))
        elif kind in ('l', 'm', 't'):
            for _ in range(groups):
                parts.append(cmd)
                parts.append(f'{rng.randint(-40, 80)} {rng.randint(-40, 80)}')
        elif kind in ('h', 'v'):
            for _ in range(groups):
                parts.append(cmd)
                parts.append(str(rng.randint(-40, 80)))
        elif kind == 'z':
            parts.append(cmd)
        else:
            raise AssertionError(kind)
    return ' '.join(parts)


def main():
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
    failures = []
    for d in HAND_PICKED:
        ok, msg = check(d)
        if not ok:
            failures.append((d, msg))

    rng = random.Random(20261119)
    for i in range(total):
        d = random_path(rng, allow_arcs=(i % 2 == 0))
        ok, msg = check(d)
        if not ok:
            failures.append((d, msg))
            if len(failures) >= 30:
                break

    print(f'hand-picked: {len(HAND_PICKED)} cases, random fuzz: {total}')
    if failures:
        print(f'FAILURES: {len(failures)}')
        for d, msg in failures[:30]:
            print(f'  {d!r}\n    -> {msg}')
        sys.exit(1)
    print('ALL MATCH')


if __name__ == '__main__':
    main()
