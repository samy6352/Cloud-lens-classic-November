"""Whether an interactive sign-in is something this process could actually complete.

`DefaultAzureCredential`'s interactive-browser step is a real convenience on a developer's
machine and a hazard everywhere else, because of how it fails: it opens a browser and waits
for somebody to sign in. When nobody is there -- App Service, a container, a CI runner -- the
call does not fail, it never returns. That is worse than no credential at all. A missing
credential raises in a second and says what is missing; this hangs whatever asked for it,
which for a web app is a request worker held open until the client gives up.

It was found on the Windows CI legs, where two tests were killed at the 300-second timeout
and the job cleaned up five orphaned msedge processes afterwards. The same code path is
reachable in production: any warehouse ingest running as the app's own identity, on a
deployment where managed identity is not configured, would sit there instead of reporting
the misconfiguration.

So the browser is offered only where a person could answer it: a real console session, with
nothing in the environment saying otherwise. `AZURE_ALLOW_INTERACTIVE_LOGIN` overrides the
guess in either direction, for the case this does not predict.
"""
from __future__ import annotations

import os
import sys

# Set by GitHub Actions and most other runners (CI), Azure Pipelines (TF_BUILD), App Service
# (WEBSITE_INSTANCE_ID), Container Apps, Kubernetes and Functions. None of them has a person
# in front of a browser.
_UNATTENDED = (
    "CI",
    "TF_BUILD",
    "WEBSITE_INSTANCE_ID",
    "CONTAINER_APP_NAME",
    "KUBERNETES_SERVICE_HOST",
    "FUNCTIONS_WORKER_RUNTIME",
)

_NO = {"0", "false", "no", "off"}


def interactive_allowed() -> bool:
    """True when a browser sign-in prompt could actually be answered."""
    override = os.getenv("AZURE_ALLOW_INTERACTIVE_LOGIN")
    if override is not None and override.strip():
        return override.strip().lower() not in _NO

    if any(os.getenv(name) for name in _UNATTENDED):
        return False

    # Every one of the three, not just stdin. Windows reports the null device as a character
    # device, so `sys.stdin.isatty()` is True for a subprocess started with stdin=DEVNULL --
    # which is exactly how the test suite runs, and how the hang got in. A redirected stream is
    # a pipe on both platforms and reports honestly, so requiring all three means a real
    # console session and nothing else.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if stream is None or not stream.isatty():
                return False
        except (AttributeError, ValueError, OSError):
            return False
    return True
