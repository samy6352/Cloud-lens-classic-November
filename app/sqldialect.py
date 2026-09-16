"""Run the application's DuckDB-flavoured SQL on PostgreSQL.

DuckDB itself is gone -- PostgreSQL is the only engine. What remains is the dialect:
a few hundred statements across two dozen query modules were written against DuckDB,
and this translates them on the way to the server. It is not a second backend and
there is nothing to choose between.

Why a translator rather than 200 edits
--------------------------------------
The dashboard, waste, tags, uptime, commitments and savings modules contain a few
hundred SQL statements written against DuckDB. Most of that SQL is already
portable -- it is ordinary aggregation over quoted column names -- but a handful
of constructs are not, and they are scattered through every one of those files:

    round(sum("BilledCost"), 2)          PostgreSQL has no round(double, int)
    strftime("ChargePeriodStart", '%Y-%m')   no strftime at all
    regexp_extract(s, p)                 spelled substring(s from p)
    regexp_matches(s, p)                 spelled s ~ p
    json_extract_string(doc, '$.k')      spelled doc #>> '{k}'
    TRY_CAST(x AS DOUBLE)                no TRY_CAST
    ?                                    placeholders are %s

Rewriting every call site would touch thousands of lines across two dozen
modules, and would have to be repeated -- wrongly, eventually -- by anyone adding
a query later. Translating in one place instead means new SQL can be written in
the dialect the codebase already uses, and there is exactly one thing to test.

The translation is deliberately narrow. It is not a general SQL rewriter: it
handles the closed set of constructs this application actually uses, and leaves
everything else untouched. Anything it does not recognise reaches PostgreSQL
verbatim and fails loudly there, which is the right outcome -- a translator that
guesses is worse than one that declines.

String literals, quoted identifiers and comments are never rewritten. That is
what makes the difference between a translator and a find-and-replace: a tag
value of `round(x, 2)` or a meter named `20 ?` is data, and passes through
unchanged.
"""
from __future__ import annotations

import re

# Format codes used by the application's strftime calls, mapped to the
# equivalent to_char template patterns. Only the ones actually used are listed:
# an unrecognised code raises rather than silently producing a different string.
_STRFTIME_CODES = {
    "%Y": "YYYY",
    "%m": "MM",
    "%d": "DD",
    "%H": "HH24",
    "%M": "MI",
    "%S": "SS",
}

_TYPE_NAMES = {
    "double": "double precision",
    "varchar": "text",
    "bigint": "bigint",
    "date": "date",
    "timestamp": "timestamp",
}


# What a protected range is, because the scanner treats the three differently.
LITERAL, IDENTIFIER, COMMENT = "literal", "identifier", "comment"


def _is_e_string(sql: str, quote: int) -> bool:
    """Whether the quote at `quote` opens an `E'...'` escape string.

    PostgreSQL honours `\\'` inside one, and nowhere else. Getting this wrong is not a
    cosmetic error: closing the span early inverts the quote parity for the rest of the
    statement, so everything after it is misclassified -- executable SQL read as a literal,
    or a literal read as SQL.

    The `E` has to be a prefix rather than the tail of an identifier, or a column called
    `price` would make the following quote look like an escape string.
    """
    if quote == 0 or sql[quote - 1] not in "Ee":
        return False
    before = quote - 2
    return before < 0 or not (sql[before].isalnum() or sql[before] == "_")


def _scan(sql: str) -> tuple[list[tuple[int, int, str]], bool]:
    """Protected ranges, and whether the statement ends inside an unterminated one.

    Single-quoted literals, `E'...'` escape strings, dollar-quoted strings, double-quoted
    identifiers, line comments and block comments. Doubled quotes inside a literal are an
    escaped quote rather than the end of it, which is what the inner loop is checking for.

    The unterminated flag exists for callers that scan the result for forbidden words. A
    statement PostgreSQL would reject as unterminated is not worth guessing at, and guessing
    is dangerous: the guess decides which half of the statement the scanner can see.
    """
    protected: list[tuple[int, int, str]] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "$":
            # $tag$ ... $tag$ -- the delimiter is whatever appears between the dollars, so
            # the closing one has to match it exactly.
            opener = re.compile(r"\$([A-Za-z_]\w*)?\$").match(sql, i)
            if opener:
                tag = opener.group(0)
                close = sql.find(tag, opener.end())
                if close == -1:
                    protected.append((i, n, LITERAL))
                    return protected, True
                protected.append((i, close + len(tag), LITERAL))
                i = close + len(tag)
                continue
            i += 1
            continue
        if ch in "'\"":
            escapes = ch == "'" and _is_e_string(sql, i)
            j, closed = i + 1, False
            while j < n:
                if escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            kind = IDENTIFIER if ch == '"' else LITERAL
            if not closed:
                protected.append((i, n, kind))
                return protected, True
            protected.append((i, j + 1, kind))
            i = j + 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            protected.append((i, j, COMMENT))
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i)
            if j == -1:
                protected.append((i, n, COMMENT))
                return protected, True
            protected.append((i, j + 2, COMMENT))
            i = j + 2
            continue
        i += 1
    return protected, False


def _spans(sql: str) -> list[tuple[int, int]]:
    """Character ranges that must never be rewritten."""
    return [(start, end) for start, end, _ in _scan(sql)[0]]


def _is_code(pos: int, protected: list[tuple[int, int]]) -> bool:
    """Whether `pos` is executable SQL rather than a literal, identifier or comment."""
    return not any(start <= pos < end for start, end in protected)


def _match_call(sql: str, open_paren: int) -> int:
    """Index of the parenthesis closing the one at `open_paren`.

    Parentheses inside literals and identifiers do not count, so a value like
    `'a)b'` cannot end a call early.
    """
    protected = _spans(sql)
    depth = 0
    for i in range(open_paren, len(sql)):
        if not _is_code(i, protected):
            continue
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses in SQL")


def _split_args(inner: str) -> list[str]:
    """Top-level comma-separated arguments of a call.

    Commas nested in a sub-expression or inside a literal belong to that
    argument, not to the call being split.
    """
    protected = _spans(inner)
    args, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        if not _is_code(i, protected):
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(inner[start:i])
            start = i + 1
    args.append(inner[start:])
    return [a.strip() for a in args]


def _rewrite_calls(sql: str, name: str, build) -> str:
    """Replace every call to `name(...)` with whatever `build(args)` returns.

    A single forward pass. Arguments are translated by recursion before the call
    around them is built, so nested calls are handled innermost-first, and the
    scanner then continues past the finished replacement rather than re-reading
    it.

    Scanning forward is not an optimisation, it is the correctness condition. An
    earlier version rescanned the whole statement after each replacement, and
    `round(x, 2)` -> `round((x)::numeric, 2)` matched itself on the next pass and
    wrapped forever.

    `build` may return None to mean "already valid, leave it as it was" -- which
    is how one-argument `round` is passed through.
    """
    pattern = re.compile(rf"\b{name}\s*\(", re.IGNORECASE)
    protected = _spans(sql)
    out: list[str] = []
    pos = 0
    while True:
        match = None
        for m in pattern.finditer(sql, pos):
            if _is_code(m.start(), protected):
                match = m
                break
        if match is None:
            out.append(sql[pos:])
            return "".join(out)

        open_paren = match.end() - 1
        close = _match_call(sql, open_paren)
        args = [_rewrite_calls(a, name, build)
                for a in _split_args(sql[open_paren + 1:close])]

        out.append(sql[pos:match.start()])
        replacement = build(args)
        if replacement is None:
            out.append(sql[match.start():open_paren + 1])
            out.append(",".join(args))
            out.append(")")
        else:
            out.append(replacement)
        pos = close + 1


def _round(args: list[str]) -> str | None:
    """`round(x, 2)` on a float.

    PostgreSQL has `round(numeric, int)` and `round(double precision)`, but not
    `round(double precision, int)` -- and every money column in the warehouse is
    a double. Casting the value to numeric picks the two-argument overload.

    The result is cast back. Without it PostgreSQL returns `Decimal` where DuckDB
    returned `float`, and the difference does not stay in the database: callers
    divide these values, compare them against floats, and hand them to the JSON
    encoder. `Decimal / float` raises TypeError, so the failure would surface far
    from here, in a dashboard tab, and only for the tabs that do arithmetic on a
    rounded column.

    Rounding is unchanged by the round trip: both engines round half away from
    zero at this scale, and a two-decimal value is exactly representable.
    """
    if len(args) != 2:
        return None
    return f"round(({args[0]})::numeric, {args[1]})::double precision"


def _strftime(args: list[str]) -> str:
    """`strftime(date, '%Y-%m')` becomes `to_char(date, 'YYYY-MM')`."""
    if len(args) != 2:
        raise ValueError(f"strftime expects 2 arguments, got {len(args)}")
    value, fmt = args
    literal = fmt.strip()
    if not (literal.startswith("'") and literal.endswith("'")):
        raise ValueError("strftime needs a literal format string to be translated")
    body = literal[1:-1]
    out, i = [], 0
    while i < len(body):
        if body[i] == "%" and i + 1 < len(body):
            code = body[i:i + 2]
            if code not in _STRFTIME_CODES:
                raise ValueError(f"unsupported strftime code {code}")
            out.append(_STRFTIME_CODES[code])
            i += 2
        else:
            # Literal text inside a to_char template must be quoted, or it is
            # read as a pattern: a stray 'D' would become the day of week.
            out.append('"' + body[i] + '"' if body[i].isalpha() else body[i])
            i += 1
    return f"to_char({value}, '{''.join(out)}')"


def _regexp_extract(args: list[str]) -> str:
    """`regexp_extract(s, p)` becomes `substring(s from p)`.

    Both return the first capture group when the pattern has one, and the whole
    match when it does not, which is the behaviour every call site here relies on.
    """
    if len(args) == 2:
        return f"substring({args[0]} from {args[1]})"
    if len(args) == 3:
        # DuckDB's third argument selects a capture group. PostgreSQL's
        # regexp_match returns them as an array.
        return f"(regexp_match({args[0]}, {args[1]}))[{args[2]}]"
    raise ValueError(f"regexp_extract expects 2 or 3 arguments, got {len(args)}")


def _regexp_matches(args: list[str]) -> str:
    """`regexp_matches(s, p)` is the `~` operator.

    Wrapped in parentheses because `~` binds less tightly than the function call
    it replaces, and these appear inside AND/OR chains.
    """
    if len(args) != 2:
        raise ValueError(f"regexp_matches expects 2 arguments, got {len(args)}")
    return f"(({args[0]}) ~ ({args[1]}))"


def _json_extract_string(args: list[str]) -> str:
    """`json_extract_string(doc, '$.k')` becomes `((doc) #>> '{k}')`.

    DuckDB's accessor takes a JSONPath and returns the value as text, already
    unquoted. PostgreSQL spells that `#>>`, whose right operand is a text array of
    path steps -- so `'$'` (the whole document) is the empty path `'{}'`, and
    `'$.k'` is `'{k}'`. Both yield a bare string rather than a quoted JSON scalar,
    which is what the tag comparison needs: `lower(...) IN (...)` against `"prod"`
    with the quotes still attached would never match.

    Only literal paths are translated. A computed path is left to fail loudly
    rather than be guessed at, and a path with a quote, brace or comma in it is
    refused outright -- it would otherwise be interpolated into an array literal
    where those characters are syntax.
    """
    if len(args) != 2:
        raise ValueError(f"json_extract_string expects 2 arguments, got {len(args)}")
    doc, path = args[0], args[1].strip()
    if not (path.startswith("'") and path.endswith("'") and len(path) >= 2):
        raise ValueError("json_extract_string needs a literal path to be translated")
    body = path[1:-1]
    steps = [s for s in body.removeprefix("$").split(".") if s]
    if any(c in body for c in "'\"{},[]*"):
        raise ValueError(f"json_extract_string path {body!r} is not supported")
    return f"(({doc}) #>> '{{{','.join(steps)}}}')"


def _try_cast(sql: str) -> str:
    """`TRY_CAST(x AS type)` becomes a call to the shim installed by the store.

    PostgreSQL has no TRY_CAST. `try_cast_double`, `try_cast_date` and
    `try_cast_json` are created
    in the database at schema initialisation; they return NULL where a plain cast
    would raise, which is what the warehouse wants -- a meter description that
    does not parse as a number is missing data, not a failed query.
    """
    pattern = re.compile(r"\btry_cast\s*\(", re.IGNORECASE)
    while True:
        protected = _spans(sql)
        found = None
        for m in pattern.finditer(sql):
            if _is_code(m.start(), protected):
                found = m
                break
        if found is None:
            return sql
        open_paren = found.end() - 1
        close = _match_call(sql, open_paren)
        inner = sql[open_paren + 1:close]
        split = re.split(r"\s+AS\s+", inner, maxsplit=1, flags=re.IGNORECASE)
        if len(split) != 2:
            raise ValueError("TRY_CAST needs an AS clause")
        value, target = split[0].strip(), split[1].strip().lower()
        if target in {"double", "double precision", "float", "real"}:
            replacement = f"try_cast_double(({value})::text)"
        elif target == "date":
            replacement = f"try_cast_date(({value})::text)"
        elif target in {"json", "jsonb"}:
            replacement = f"try_cast_json(({value})::text)"
        else:
            raise ValueError(f"TRY_CAST to {target} is not supported")
        sql = sql[:found.start()] + replacement + sql[close + 1:]


def _interval(sql: str) -> str:
    """`INTERVAL 30 DAY` becomes `INTERVAL '30 day'`.

    DuckDB accepts a bare number and a bare unit keyword; PostgreSQL requires the
    whole thing as a string literal and rejects `INTERVAL 30 DAY` with a syntax
    error at the number. Every dashboard window is built this way -- `date '...' -
    INTERVAL 30 DAY` -- so without this the cost breakdown, tags, uptime, AKS and
    commitments panels all return 500 on PostgreSQL while the header totals, which
    use no interval, carry on working. That combination reads as one broken panel
    rather than as a missing translation.

    Only whole numbers of a known unit are rewritten. An expression -- `INTERVAL
    (x) DAY` -- is left alone to fail loudly rather than be guessed at.
    """
    units = "day|days|month|months|year|years|hour|hours|minute|minutes|second|seconds"
    pattern = re.compile(rf"\bINTERVAL\s+(\d+)\s+({units})\b", re.IGNORECASE)
    protected = _spans(sql)

    def swap(m: re.Match) -> str:
        if not _is_code(m.start(), protected):
            return m.group(0)
        unit = m.group(2).lower().rstrip("s")
        return f"INTERVAL '{m.group(1)} {unit}'"

    return pattern.sub(swap, sql)


def _placeholders(sql: str) -> str:
    """`?` becomes `%s`, and a literal `%` is doubled so psycopg leaves it alone."""
    protected = _spans(sql)
    out = []
    for i, ch in enumerate(sql):
        if ch == "%":
            # Inside a literal too: psycopg scans the whole string for markers,
            # so a LIKE pattern like '%prod%' has to be escaped as well.
            out.append("%%")
        elif ch == "?" and _is_code(i, protected):
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def _questions_to_markers(sql: str) -> str:
    """`?` becomes `%s`, leaving `%` alone."""
    protected = _spans(sql)
    return "".join("%s" if ch == "?" and _is_code(i, protected) else ch
                   for i, ch in enumerate(sql))


def strip_literals(sql: str) -> str:
    """`sql` with string literals and comments blanked out, ready to be scanned for keywords.

    Spaces replace the characters rather than removing them, so the result is the same
    length as the input and an offset into one is an offset into the other.

    For callers that scan SQL for forbidden words. A meter really is called `Load Balancer`
    and a resource group really can be named `delete-me`, so a scanner that reads the whole
    statement rejects legitimate questions about them -- caution that produces a wrong
    answer. Only executable SQL can carry a keyword, so only executable SQL is scanned.

    Quoted identifiers keep their text and lose only their quotes, which is the opposite of
    what they need for rewriting. `"pg_sleep_for"(...)` is an ordinary call -- quoting a
    lower-case name changes nothing about how PostgreSQL resolves it -- so blanking it would
    hide a function from the scanner. `"public".costs` becomes ` public .costs` for the same
    reason: the schema is still there, so a check looking for it still finds it.

    Raises on an unterminated literal instead of returning a best guess. An unterminated
    quote flips the parity of everything after it, so the blanked text would hide whichever
    half the scanner most needed to see -- and PostgreSQL would reject the statement anyway.
    """
    protected, unterminated = _scan(sql)
    if unterminated:
        raise ValueError("unterminated string, identifier or comment in SQL")
    out = list(sql)
    for start, end, kind in protected:
        if kind == IDENTIFIER:
            # Only the delimiters, so the name itself stays scannable.
            for edge in (start, end - 1):
                if 0 <= edge < len(out):
                    out[edge] = " "
            continue
        for i in range(start, min(end, len(out))):
            out[i] = " "
    return "".join(out)


def to_postgres(sql: str, bind: bool = True) -> str:
    """The application's SQL, in a form PostgreSQL accepts.

    `bind` says whether the caller is passing parameters, and it decides how `%`
    is treated. psycopg only interprets `%` as a placeholder marker when
    parameters are supplied; with none, the string is sent verbatim. So escaping
    unconditionally is wrong in the parameterless case -- `i % 365` was sent as
    `i %% 365`, which PostgreSQL rejects with "operator does not exist: integer
    %% integer", and `LIKE '/subscriptions/%'` became `LIKE '/subscriptions/%%'`
    and only survived because two wildcards happen to match what one does.

    Placeholder rewriting is last either way: every step above may introduce `%`
    characters of its own -- to_char templates, regex patterns -- and those need
    escaping exactly once, along with the ones already in the statement.
    """
    sql = _rewrite_calls(sql, "strftime", _strftime)
    sql = _rewrite_calls(sql, "regexp_extract", _regexp_extract)
    sql = _rewrite_calls(sql, "regexp_matches", _regexp_matches)
    sql = _rewrite_calls(sql, "json_extract_string", _json_extract_string)
    sql = _try_cast(sql)
    sql = _rewrite_calls(sql, "round", _round)
    sql = _types(sql)
    sql = _interval(sql)
    return _placeholders(sql) if bind else _questions_to_markers(sql)


def _types(sql: str) -> str:
    """DuckDB type names in DDL and casts, in PostgreSQL's spelling.

    `DOUBLE` is only rewritten when it is not already followed by `precision`.
    Earlier steps emit `::double precision` casts of their own, and rewriting the
    first word of those produced `double precision precision`.
    """
    protected = _spans(sql)

    def swap(m: re.Match) -> str:
        if not _is_code(m.start(), protected):
            return m.group(0)
        return _TYPE_NAMES.get(m.group(0).lower(), m.group(0))

    return re.sub(r"\b(DOUBLE(?!\s+precision\b)|VARCHAR)\b", swap, sql,
                  flags=re.IGNORECASE)


# Where the shim functions live.
#
# A schema of their own, rather than alongside the warehouse tables, because the two have
# different exposure. Model-authored SQL runs with the application schema hidden -- that is
# what stops it reaching `costs_rollup`, which is not scope-filtered -- but it still has to be
# able to call these. A schema holding nothing but three immutable functions is safe to leave
# on that search path; the one holding the cost data is not.
#
# pg_temp would have been simpler and does not work: PostgreSQL consults the temporary schema
# for relation and type names only, never for functions, so a temp copy can only be called
# fully qualified and the translator emits unqualified names.
SHIM_SCHEMA = "cloudlens_fn"

# SQL installed into the database once, so the translations above have somewhere
# to land. All three functions are IMMUTABLE and STRICT: PostgreSQL may then fold them
# into an index scan rather than calling them per row.
SHIMS = f"""
CREATE SCHEMA IF NOT EXISTS {SHIM_SCHEMA};

-- PARALLEL RESTRICTED, not SAFE, and that distinction is not cosmetic.
--
-- Each of these catches a failed cast with an EXCEPTION block, and in PL/pgSQL an EXCEPTION
-- block opens a subtransaction. A parallel worker may not start one, so under a parallel plan
-- the query dies with "cannot start subtransactions during a parallel operation" — and whether
-- the planner chooses a parallel scan depends on how much data is in the table. Declared SAFE,
-- these worked on a small warehouse and failed on a large one, which is the worst way for a
-- fault to be distributed: it appears in production and not in development.
--
-- RESTRICTED keeps the plan parallel and pins these functions to the leader process, where a
-- subtransaction is allowed. UNSAFE would also be correct and would cost the parallelism.
CREATE OR REPLACE FUNCTION {SHIM_SCHEMA}.try_cast_double(value text)
RETURNS double precision
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL RESTRICTED AS $$
BEGIN
    RETURN value::double precision;
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION {SHIM_SCHEMA}.try_cast_date(value text)
RETURNS date
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL RESTRICTED AS $$
BEGIN
    RETURN value::date;
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$;

-- Anything that is not a JSON *object* reads as NULL, not just anything that
-- fails to parse. The only caller feeds the result to json_each, and PostgreSQL
-- raises "cannot deconstruct an array as an object" on a valid JSON array --
-- which would fail the whole query rather than the one row. json_each over NULL
-- returns no rows, so a resource whose Tags are absent, malformed or an array
-- simply has no tags, which is what the tag filters mean.
CREATE OR REPLACE FUNCTION {SHIM_SCHEMA}.try_cast_json(value text)
RETURNS json
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL RESTRICTED AS $$
DECLARE
    parsed json;
BEGIN
    parsed := value::json;
    IF json_typeof(parsed) <> 'object' THEN
        RETURN NULL;
    END IF;
    RETURN parsed;
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$;
"""
