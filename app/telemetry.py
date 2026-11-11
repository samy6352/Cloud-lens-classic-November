"""Opt-in Azure Monitor instrumentation; no request bodies or identity headers."""
from __future__ import annotations

import logging
import os

_configured = False


def configure() -> bool:
    global _configured
    connection = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection:
        return False
    if _configured:
        return True
    from azure.identity import DefaultAzureCredential
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=connection,
        credential=DefaultAzureCredential(
            managed_identity_client_id=os.getenv("AZURE_CLIENT_ID") or None,
            exclude_interactive_browser_credential=True,
        ),
        logger_name="cloudlens",
        enable_live_metrics=False,
        instrumentation_options={
            "fastapi": {"enabled": False},
        },
    )
    _configured = True
    logging.getLogger("cloudlens.telemetry").info("Azure Monitor telemetry enabled")
    return True


def instrument(app) -> None:
    if not configure():
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/auth/.*,/login,/logout",
        http_capture_headers_server_request=[],
        http_capture_headers_server_response=[],
    )
