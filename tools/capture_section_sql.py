"""Dump the full SQL of every query a section tab issues, slowest first.

Scratch tool for the work_mem/collation investigation: `profile_section` truncates
each statement to 150 characters, which is enough to tell queries apart but not
enough to replay one under EXPLAIN.
"""
from __future__ import annotations

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)

# `app.warehouse` builds a Warehouse at import time, so the connection settings have to
# be in the environment before that import. The application gets them from `.env` via
# `app.main`, which is far too much to import for a read-only profiling run.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from app import dashboard as dash_mod  # noqa: E402
from app import warehouse as wh_mod  # noqa: E402

TIMINGS: list[tuple[float, str]] = []

_real_reader = wh_mod.Warehouse.reader


class TimingReader:
    def __init__(self, inner):
        self._inner = inner

    def rows(self, sql, *a, **kw):
        t0 = time.monotonic()
        try:
            return self._inner.rows(sql, *a, **kw)
        finally:
            TIMINGS.append((time.monotonic() - t0, sql))

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
days = int(sys.argv[2]) if len(sys.argv) > 2 else 30

t0 = time.monotonic()
d.section(target, None, days)
total = time.monotonic() - t0

print(f"section({target}, {days}d) total {total:.2f}s over {len(TIMINGS)} queries")
for i, (took, sql) in enumerate(sorted(TIMINGS, key=lambda p: -p[0])[:3]):
    print(f"\n{'=' * 78}\n#{i}  {took:.2f}s\n{'=' * 78}\n{sql}")
