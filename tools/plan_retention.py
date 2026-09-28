"""Inventory/preview only: python tools\\plan_retention.py --help."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retention import (  # noqa: E402
    build_plan,
    connect_read_only,
    inventory,
    render_text,
    validate_policy,
    validate_schema,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only inventory and retention PREVIEW; never changes data.",
        epilog="No apply/delete/archive option exists. keep-months is NOT an enforced policy. "
               "Capacity projects additional incoming months while preserving all existing history.",
    )
    parser.add_argument("--keep-months", type=int, default=None,
                        help="Preview N completed calendar months PLUS current/future; "
                             "unset preserves all history")
    parser.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today(),
                        help="Calendar date YYYY-MM-DD (default local today)")
    parser.add_argument("--headroom-gib", type=int, default=None,
                        help="Nonnegative user allowance for peak replacement/temp/backup; "
                             "unset reports unmeasured, not zero")
    parser.add_argument("--schema", help="Warehouse schema (default PGSCHEMA or public)")
    parser.add_argument("--env-file", type=Path,
                        help="Explicit dotenv file; relative paths resolve from repository root. "
                             "Default CLOUDLENS_ENV_FILE or repository .env; shell values win")
    parser.add_argument("--no-env", action="store_true", help="Do not load a dotenv file")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        validate_policy(args.keep_months, args.as_of)
        if args.headroom_gib is not None and args.headroom_gib < 0:
            raise ValueError("headroom must be nonnegative")
        if not args.no_env:
            from dotenv import load_dotenv

            path = args.env_file or Path(os.getenv("CLOUDLENS_ENV_FILE") or ROOT / ".env")
            load_dotenv(path if path.is_absolute() else ROOT / path, override=False)
        schema = validate_schema(args.schema or os.getenv("PGSCHEMA", "").strip() or "public")
        with connect_read_only() as connection:
            snapshot = inventory(connection, schema)
        plan = build_plan(
            snapshot, as_of=args.as_of, keep_months=args.keep_months,
            headroom_bytes=args.headroom_gib * 1024**3 if args.headroom_gib is not None else None,
        )
        print(json.dumps(plan, indent=2) if args.format == "json" else render_text(plan))
        return 0
    except Exception as exc:
        # Connection/auth exception strings can contain endpoints or credentials.
        print(json.dumps({"mode": "PREVIEW ONLY - no data changed", "error_type": type(exc).__name__,
                          "error": "Planning failed; check options, connection, permissions and "
                                   "catalogue consistency. No credentials or DSN are reported."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
