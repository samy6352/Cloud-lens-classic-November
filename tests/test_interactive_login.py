"""The interactive browser sign-in must not be offered where nobody can answer it.

`DefaultAzureCredential(exclude_interactive_browser_credential=False)` opens a browser and
blocks until a person signs in. That is fine at a developer's terminal and is the wrong
failure mode everywhere else, because it is not a failure: the call never returns. A missing
credential raises in about a second and names what is missing; this hangs whatever asked for
it, which in the app is a request worker and in CI was two tests killed at the 300-second
timeout, with five orphaned msedge processes swept up after the job.

Run: python test_interactive_login.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os  # noqa: E402

from app.identity import interactive_allowed  # noqa: E402

HERE = Path(__file__).resolve().parents[1]

checks = 0
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


class _Env:
    """Set and restore environment variables around one check."""

    def __init__(self, **values: str | None) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


CLEAR = dict.fromkeys(
    ("CI", "TF_BUILD", "WEBSITE_INSTANCE_ID", "CONTAINER_APP_NAME",
     "KUBERNETES_SERVICE_HOST", "FUNCTIONS_WORKER_RUNTIME",
     "AZURE_ALLOW_INTERACTIVE_LOGIN"))

print("\nwhere a browser prompt may be offered")

# This test is itself run with stdin closed and its output captured, so the unattended answer
# is the default. Note that `stdin` alone would not settle it: Windows reports the null device
# as a terminal, which is how a hang got into CI in the first place.
with _Env(**CLEAR):
    check("no console session, no browser", interactive_allowed() is False)

for marker in ("CI", "TF_BUILD", "WEBSITE_INSTANCE_ID", "CONTAINER_APP_NAME",
               "KUBERNETES_SERVICE_HOST", "FUNCTIONS_WORKER_RUNTIME"):
    with _Env(**{**CLEAR, marker: "1"}):
        check(f"{marker} means unattended", interactive_allowed() is False)

with _Env(**{**CLEAR, "AZURE_ALLOW_INTERACTIVE_LOGIN": "1"}):
    check("an explicit yes is honoured", interactive_allowed() is True)

with _Env(**{**CLEAR, "AZURE_ALLOW_INTERACTIVE_LOGIN": "1", "CI": "true"}):
    check("and outranks the runner's own marker", interactive_allowed() is True)

for no in ("0", "false", "no", "off", "FALSE"):
    with _Env(**{**CLEAR, "AZURE_ALLOW_INTERACTIVE_LOGIN": no}):
        check(f"an explicit {no!r} is honoured", interactive_allowed() is False)

with _Env(**{**CLEAR, "AZURE_ALLOW_INTERACTIVE_LOGIN": "  "}):
    check("a blank override is ignored rather than read as a yes",
          interactive_allowed() is False)


# ------------------------------------------------- no module may hardcode the browser on
print("\nno credential is built with the browser unconditionally enabled")

# The literal this looks for is the bug: `exclude_...=False` offers the browser whatever the
# process is. Every site must ask instead, so that adding one back is a test failure rather
# than a hang somebody has to diagnose from an orphaned browser process in a CI log.
HARDCODED = re.compile(r"exclude_interactive_browser_credential\s*=\s*False")

for path in sorted((HERE / "app").glob("*.py")):
    body = path.read_text(encoding="utf-8")
    check(f"{path.name} does not force it on", not HARDCODED.search(body))

users = [p.name for p in sorted((HERE / "app").glob("*.py"))
         if "exclude_interactive_browser_credential" in p.read_text(encoding="utf-8")]
check("the sites that offer a browser all ask first",
      all("interactive_allowed" in (HERE / "app" / name).read_text(encoding="utf-8")
          for name in users), f"sites: {users}")
check("and there is at least one, so this is not passing vacuously", bool(users))


print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
sys.exit(1 if failures else 0)
