import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from app import telemetry


def test_telemetry_is_opt_in(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(telemetry, "_configured", False)
    assert telemetry.configure() is False


def test_configured_telemetry_fails_explicitly(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "configured")
    monkeypatch.setattr(telemetry, "_configured", False)
    module = ModuleType("azure.monitor.opentelemetry")
    module.configure_azure_monitor = Mock(side_effect=ValueError("Invalid connection string"))
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", module)
    with pytest.raises(ValueError, match="Invalid connection"):
        telemetry.configure()
    assert not telemetry._configured


def test_telemetry_instruments_app_without_identity_headers(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "configured")
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setenv("AZURE_CLIENT_ID", "workload-identity")
    credential = Mock()
    credential_type = Mock(return_value=credential)
    import azure.identity
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", credential_type)
    monitor = ModuleType("azure.monitor.opentelemetry")
    monitor.configure_azure_monitor = Mock()
    instrumentor = Mock()
    fastapi = ModuleType("opentelemetry.instrumentation.fastapi")
    fastapi.FastAPIInstrumentor = SimpleNamespace(instrument_app=instrumentor)
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", monitor)
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.fastapi", fastapi)
    app = object()
    telemetry.instrument(app)
    monitor.configure_azure_monitor.assert_called_once()
    assert monitor.configure_azure_monitor.call_args.kwargs["credential"] is credential
    credential_type.assert_called_once_with(
        managed_identity_client_id="workload-identity",
        exclude_interactive_browser_credential=True,
    )
    instrumentor.assert_called_once_with(
        app, excluded_urls="/auth/.*,/login,/logout",
        http_capture_headers_server_request=[], http_capture_headers_server_response=[],
    )


def test_real_instrumentation_emits_request_spans_without_exporting():
    script = """
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from app import telemetry
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
telemetry.configure = lambda: True
app = FastAPI()
telemetry.instrument(app)
@app.get('/probe')
def probe():
    return {'ok': True}
with TestClient(app) as client:
    assert client.get('/probe').status_code == 200
spans = exporter.get_finished_spans()
assert any(span.kind.name == 'SERVER' for span in spans)
assert all('http.request.header.authorization' not in span.attributes for span in spans)
provider.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
