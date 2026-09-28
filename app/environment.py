"""One definition of the legacy environment tag, including its JSON semantics."""
from __future__ import annotations

import os
from collections.abc import Sequence


def enabled() -> bool:
    value = os.getenv("CLOUDLENS_ENV_ROLLUP", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("CLOUDLENS_ENV_ROLLUP must be true or false")
    return value == "true"


ENABLED = enabled()
COLUMN = "Environment"


def tag_sql(document: str = 'TRY_CAST("Tags" AS JSON)') -> str:
    # Empty strings win; only SQL/JSON null advances to the next spelling.
    return "COALESCE(" + ", ".join(
        f"{document} #>> '{{{key}}}'"
        for key in ("env", "Env", "environment", "Environment")
    ) + ", '')"


RAW_SQL = f"lower({tag_sql()})"


def source_columns(columns: Sequence[str]) -> list[str]:
    """Resolve computed columns to their actual raw dependencies for validation."""
    return list(dict.fromkeys("Tags" if c == COLUMN else c for c in columns))


def stored_where(where: str) -> str:
    """Only called after checking the summary's grain and durable readiness."""
    if RAW_SQL not in where:
        return where
    where = where.replace(RAW_SQL, f'"{COLUMN}"')
    # Preserve bound values and membership, but evaluate each small parameter set
    # once. Temp-table semijoins badly underestimate combined filters and choose
    # repeated wide sorts: a measured complete Billing statement exceeded 110 s
    # with env+service, versus 35 s with these equivalent array initplans.
    for key in ("env", "rg", "service", "region", "charge", "pricing"):
        query = f"SELECT val FROM _filter WHERE dim = '{key}'"
        where = where.replace(f"IN ({query})", f"= ANY(ARRAY({query}))")
    return where.replace("IN (SELECT id FROM _scope)",
                         "= ANY(ARRAY(SELECT id FROM _scope))")


def project(source: str, columns: Sequence[str]) -> str:
    """Narrow inline projection; lateral barriers evaluate JSON once per row.

    Keep normalization inside the second scalar boundary too: PostgreSQL can
    memoize the compact result for repeated Tags rather than caching a large
    document and repeating its path extraction for every charge. Neither boundary
    encloses the raw relation, so outer date predicates still prune partitions.
    """
    selected = ", ".join(f'"{c}"' for c in columns if c != COLUMN)
    return (
        f'(SELECT {selected}, _environment_value."{COLUMN}" '
        f'FROM {source} CROSS JOIN LATERAL '
        f'(SELECT lower({tag_sql("_environment_json.doc")}) AS "{COLUMN}" FROM '
        '(SELECT TRY_CAST("Tags" AS JSON) AS doc OFFSET 0) AS _environment_json '
        'OFFSET 0) AS _environment_value) '
        'AS environment_source'
    )
