"""Time every individual query a section tab issues, to find what still dominates."""
from __future__ import annotations

import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)

from app import dashboard as dash_mod  # noqa: E402
from app import warehouse as wh_mod  # noqa: E402

TIMINGS: list[tuple[float, str]] = []


def _shorten(sql: str) -> str:
    one = re.sub(r"\s+", " ", sql).strip()
    return one[:150]


_real_reader = wh_mod.Warehouse.reader


class TimingReader:
    """Wraps a reader so every `rows` call is timed, passing everything else through."""

    def __init__(self, inner):
        self._inner = inner

    def rows(self, sql, *a, **kw):
        t0 = time.monotonic()
        try:
            return self._inner.rows(sql, *a, **kw)
        finally:
            TIMINGS.append((time.monotonic() - t0, _shorten(sql)))

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _TimingCtx:
    def __init__(self, ctx):
        self._ctx = ctx

    def __enter__(self):
        return TimingReader(self._ctx.__enter__())

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


wh_mod.Warehouse.reader = lambda self, *a, **kw: _TimingCtx(_real_reader(self, *a, **kw))

d = dash_mod.Dashboard()
target = sys.argv[1] if len(sys.argv) > 1 else "compute"

t0 = time.monotonic()
d.section(target, None, 30)
total = time.monotonic() - t0

print(f"\nsection({target}, 30d) total {total:.2f}s over {len(TIMINGS)} queries\n")
for took, sql in sorted(TIMINGS, reverse=True):
    print(f"{took:7.2f}s  {sql}")
print(f"\naccounted for: {sum(t for t, _ in TIMINGS):.2f}s of {total:.2f}s")
