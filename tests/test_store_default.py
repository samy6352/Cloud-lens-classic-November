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
from unittest.mock import MagicMock, patch

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


@pytest.mark.parametrize("value", ["yes", "1", "", "invalid"])
def test_preprovisioned_mode_rejects_invalid_configuration(monkeypatch, value):
    monkeypatch.setenv("PGHOST", "example.invalid")
    monkeypatch.setenv("PGSCHEMA", "app_private")
    monkeypatch.setenv("PG_SCHEMA_PREPROVISIONED", value)
    with pytest.raises(ValueError, match="must be true or false"):
        PostgresStore()


def test_preprovisioned_mode_requires_explicit_schema(monkeypatch):
    monkeypatch.setenv("PGHOST", "example.invalid")
    monkeypatch.delenv("PGSCHEMA", raising=False)
    monkeypatch.setenv("PG_SCHEMA_PREPROVISIONED", "true")
    with pytest.raises(ValueError, match="explicit PGSCHEMA"):
        PostgresStore()


def configured_store(monkeypatch, *, preprovisioned=True):
    monkeypatch.setenv("PGHOST", "example.invalid")
    monkeypatch.setenv("PGSCHEMA", "APP_Private")
    monkeypatch.setenv("PG_SCHEMA_PREPROVISIONED", str(preprovisioned).lower())
    monkeypatch.setenv("PGPASSWORD", "synthetic-test-only")
    monkeypatch.delenv("PG_WORK_MEM", raising=False)
    return PostgresStore()


def test_preprovisioned_open_checks_folded_schema_without_creating_it(monkeypatch):
    store = configured_store(monkeypatch)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (True, True)
    with patch("psycopg.connect", return_value=connection):
        assert store._open() is connection
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert not any("CREATE SCHEMA" in sql for sql in statements)
    assert cursor.execute.call_args_list[0].args[1] == ("app_private",)
    assert "SET search_path TO APP_Private, public, cloudlens_fn" in statements
    connection.close.assert_not_called()


@pytest.mark.parametrize("privileges", [None, (False, True), (True, False)])
def test_preprovisioned_open_fails_closed_and_closes_connection(monkeypatch, privileges):
    store = configured_store(monkeypatch)
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = privileges
    with (patch("psycopg.connect", return_value=connection),
          pytest.raises(RuntimeError, match="must exist")):
        store._open()
    connection.close.assert_called_once()


def test_default_mode_still_creates_configured_schema(monkeypatch):
    store = configured_store(monkeypatch, preprovisioned=False)
    connection = MagicMock()
    with patch("psycopg.connect", return_value=connection):
        store._open()
    statements = [call.args[0] for call in
                  connection.cursor.return_value.__enter__.return_value.execute.call_args_list]
    assert "CREATE SCHEMA IF NOT EXISTS APP_Private" in statements


def test_preprovisioned_shims_are_verified_without_ddl(monkeypatch):
    store = configured_store(monkeypatch)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [
        (12.5, None, "2026-01-01", None, {"k": "v"}, None, None), None]
    store._install_shims(connection)
    assert store._shims_installed
    assert all(call.args[0].startswith("SELECT ") for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("rows", [
    [(12.5, None, "2026-01-01", None, {"k": "v"}, None, None), (1,)],
    [(1.0, None, "2026-01-01", None, {"k": "v"}, None, None)],
])
def test_preprovisioned_shims_refuse_tables_or_wrong_semantics(monkeypatch, rows):
    store = configured_store(monkeypatch)
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchone.side_effect = rows
    with pytest.raises(RuntimeError, match="Pre-provisioned"):
        store._install_shims(connection)
    assert not store._shims_installed


def test_preprovisioned_shim_permission_error_is_not_swallowed(monkeypatch):
    import psycopg

    store = configured_store(monkeypatch)
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = (
        psycopg.errors.InsufficientPrivilege("synthetic shim denial"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        store._install_shims(connection)
    assert not store._shims_installed
