import itertools
import random
import sys

sys.path.insert(0, '/Users/lichao/.local/share/user-feedback/graphics/20261117/render-20261117-12592-path-smooth/work')
from pathref import reference_path, cairosvg_path, paths_equivalent

# --- targeted cases covering every eligibility/reset rule ---
TARGETED = [
    # smooth after cubic: explicit control vs reflected control
    'M10 80 C 40 10, 65 10, 95 80 S 150 150, 180 80',
    'M10 80 C 40 10, 65 10, 95 80 S 150 150, 180 80 S 200 20, 240 80',
    'm10 80 c 30 -70 55 -70 85 0 s 55 70 85 0',
    'M10 80 c 30 -70 55 -70 85 0 S 150 150 180 80',
    'm10 80 C 40 10 65 10 95 80 s 55 70 85 0',
    # smooth with no eligible predecessor: control = current point
    'M10 80 S 150 150, 180 80',
    'm10 80 s 140 70 170 0',
    'M10 80 T 180 80',
    'm10 80 t 170 0',
    # line interrupts the reflection
    'M10 80 C 40 10, 65 10, 95 80 L 120 80 S 150 150, 180 80',
    'M10 80 C 40 10, 65 10, 95 80 H 120 S 150 150, 180 80',
    'M10 80 C 40 10, 65 10, 95 80 V 200 S 150 150, 180 80',
    'M10 80 C 40 10, 65 10, 95 80 l 25 0 S 150 150, 180 80',
    'M10 80 C 40 10, 65 10, 95 80 h 25 s 30 70 60 0',
    'M10 80 C 40 10, 65 10, 95 80 v 120 s 30 -50 60 -120',
    # quadratic interrupts cubic smoothness and vice versa
    'M10 80 C 40 10, 65 10, 95 80 Q 140 20, 180 80 S 220 150, 260 80',
    'M10 80 Q 40 10, 95 80 T 180 80',
    'M10 80 Q 40 10, 95 80 L 140 80 T 180 80',
    'M10 80 Q 40 10, 95 80 C 120 150, 150 150, 180 80 T 260 80',
    'm10 80 q 30 -70 85 0 t 85 0 t 60 0',
    'M10 80 Q 40 10, 95 80 t 85 0',
    'm10 80 Q 40 10 95 80 T 180 80',
    # move starts a new subpath: no shared reflection state
    'M10 80 C 40 10, 65 10, 95 80 S 150 150, 180 80 M 200 80 S 280 10, 320 80',
    'M10 80 Q 40 10, 95 80 T 180 80 M 200 80 T 320 80',
    # close resets; after Z a smooth must default to current point
    'M10 80 C 40 10, 65 10, 95 80 S 120 120, 120 80 Z S 200 20, 240 80',
    'M10 80 Q 40 10, 95 80 T 160 80 Z T 240 80',
    'M10 80 C 40 10, 65 10, 95 80 Z M 10 200 S 90 280, 180 200',
    # repeated coordinate groups (implicit command repetition)
    'M10 80 S 60 20 95 80 S 150 150 180 80 S 220 20 260 80',
    'm10 80 s 50 -60 85 0 s 55 70 85 0',
    'M10 80 T 95 80 T 180 80 T 265 80',
    'm10 80 t 85 0 t 85 0',
    'M10 80 C 40 10 65 10 95 80 C 120 150 150 150 180 80',
    'M10 80 Q 40 10 95 80 Q 140 150 180 80 T 260 80',
    # arc interrupts
    'M10 80 C 40 10, 65 10, 95 80 A 30 30 0 0 1 150 80 S 220 10, 260 80',
    'M10 80 Q 40 10, 95 80 A 30 30 0 0 1 150 80 T 260 80',
]


def run(d):
    try:
        ref = reference_path(d)
    except ValueError as e:
        return None, str(e)
    try:
        got = cairosvg_path(d)
    except Exception as e:
        return False, f'raised {type(e).__name__}: {e}'
    return paths_equivalent(ref, got, tol=1e-7)


fails = 0
skipped = 0
for n, d in enumerate(TARGETED):
    ok, msg = run(d)
    if ok is None:
        skipped += 1
        print(f'TARGETED SKIP [{n}] (reference limitation): {msg}')
        continue
    if not ok:
        fails += 1
        print(f'TARGETED FAIL [{n}] d={d!r}\n  {msg}')
print(f'targeted: {len(TARGETED) - fails - skipped}/{len(TARGETED) - skipped} pass')

# --- fuzz: random sequences over the supported commands ---
random.seed(319)
CURVE_CMDS = ['C', 'c', 'S', 's', 'Q', 'q', 'T', 't']
LINE_CMDS = ['L', 'l', 'H', 'h', 'V', 'v']
BREAK_CMDS = ['M', 'm', 'Z', 'z']


def rpt():
    return round(random.uniform(-100, 200), 2)


def args_for(c):
    n = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4,
         'Q': 4, 'T': 2, 'Z': 0}[c.upper()]
    return [rpt() for _ in range(n)]


fuzz_fails = []
for trial in range(4000):
    parts = ['M', str(rpt()), str(rpt())]
    for _ in range(random.randint(1, 8)):
        pool = CURVE_CMDS + LINE_CMDS + BREAK_CMDS
        c = random.choice(pool)
        parts.append(c)
        if c not in 'Zz':
            # sometimes repeat coordinate groups
            groups = random.randint(1, 3)
            for _g in range(groups):
                parts.extend(str(v) for v in args_for(c))
    d = ' '.join(parts)
    ok, msg = run(d)
    if ok is None:
        continue
    if not ok:
        fuzz_fails.append((d, msg))
        if len(fuzz_fails) <= 10:
            print(f'FUZZ FAIL d={d!r}\n  {msg}')
print(f'fuzz: {4000 - len(fuzz_fails)}/4000 pass')
sys.exit(1 if fails or fuzz_fails else 0)
