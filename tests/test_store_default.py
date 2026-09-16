"""The app refuses to start without a database rather than inventing one.

The storage engine used to be a choice, and the default was an embedded file. An App
Service provisioned with a PostgreSQL server but no `COST_STORE` ran that file instead
-- data inside the compute, nothing for a DBA to back up, and no outward sign that
anything was wrong. cost-agent-6no73w was in exactly that state.

There is no choice any more: PostgreSQL is the only engine, so that particular
misconfiguration cannot be expressed. What remains is the case it turned into -- a
deployment that reaches this code with no server configured -- and the property worth
holding onto is that it says so at startup instead of quietly succeeding.

These tests exist because the failure they prevent is invisible: nothing crashes, the
dashboard loads, and the numbers look right.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import pytest

from app.store import PostgresStore


def test_without_a_host_it_refuses_to_start_rather_than_guessing(monkeypatch):
    """Name the missing setting, rather than falling back to something local.

    A default that "just works" here is precisely what hid the old fault: there is no
    correct guess for where a customer's warehouse lives.
    """
    monkeypatch.delenv("PGHOST", raising=False)

    with pytest.raises(RuntimeError) as raised:
        PostgresStore()
    assert "PGHOST" in str(raised.value)


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_host_is_not_a_host(monkeypatch, value):
    """A blank value is a deployment that meant to set this and did not.

    Treating it as present would get as far as a connection attempt to the empty string,
    which fails much later and says nothing about the setting that caused it.
    """
    monkeypatch.setenv("PGHOST", value)

    with pytest.raises(RuntimeError) as raised:
        PostgresStore()
    assert "PGHOST" in str(raised.value)


def test_a_schema_that_is_not_an_identifier_is_refused(monkeypatch):
    """`PGSCHEMA` is interpolated into SET search_path, which takes no parameters.

    So it is validated rather than quoted, and the check has to happen at construction --
    by the time it reaches the statement there is nothing left to reject it with.
    """
    monkeypatch.setenv("PGHOST", "example.postgres.database.azure.com")
    monkeypatch.setenv("PGSCHEMA", "public; DROP TABLE costs")

    with pytest.raises(RuntimeError) as raised:
        PostgresStore()
    assert "PGSCHEMA" in str(raised.value)


def test_a_plain_schema_name_is_accepted(monkeypatch):
    monkeypatch.setenv("PGHOST", "example.postgres.database.azure.com")
    monkeypatch.setenv("PGSCHEMA", "t_some_test_42")

    assert PostgresStore().schema == "t_some_test_42"
