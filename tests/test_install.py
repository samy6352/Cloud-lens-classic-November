"""Offline contract for the public guide, portal template and unchanged auth boundary."""
import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.offline


@pytest.fixture
def client(monkeypatch):
    for key in ("AUTH_DISABLED", "AUTH_PASSWORD", "AUTH_USERS", "AUTH_CLIENT_ID",
                "AUTH_TENANT_ID", "AUTH_CLIENT_SECRET", "AUTH_ALLOW_LOCAL"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("AUTH_SECRET", "install-test-secret-not-for-production")
    from app import main

    monkeypatch.setattr(main, "_auth", None)
    # No lifespan: the guide must not need a database, Azure or an agent.
    return TestClient(main.app, follow_redirects=False)


def test_install_public_and_csp(client):
    response = client.get("/install")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    block = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
    policy = response.headers["content-security-policy"]
    assert f"'sha256-{digest}'" in policy
    assert "'unsafe-inline'" not in policy.split("script-src ", 1)[1].split(";")[0]
    assert html.index("<script>") < html.index('rel="stylesheet"')
    assert 'get("scoutTheme")' in block
    assert "prefers-color-scheme: dark" in block
    assert client.get("/assets/install.css").status_code == 200


@pytest.mark.parametrize("path", [
    "/", "/install/", "/install/private", "/install.html", "/api/overview",
    "/api/dashboard/sections", "/api/ask", "/api/install",
])
def test_install_does_not_open_other_paths(client, path):
    response = client.get(path, headers={"accept": "application/json"})
    assert response.status_code == 401
    assert response.json()["auth"] == "required"


def test_dashboard_still_redirects_to_login(client):
    response = client.get("/", headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert client.get("/login").status_code == 200


def test_install_markup_and_subscription_template():
    html = (ROOT / "web" / "install.html").read_text(encoding="utf-8")
    href = re.search(r'id="deploy-azure" href="([^"]+)"', html).group(1)
    assert unquote(href) == (
        "https://portal.azure.com/#create/Microsoft.Template/uri/"
        "https://cloudlens-rnabu.azurewebsites.net/install/template"
    )
    commands = re.search(r"<pre[^>]*><code>(.*?)</code></pre>", html, re.S).group(1)
    assert commands.splitlines() == [
        "git clone https://github.com/samy6352/Cloud-lens-classic-November.git",
        "cd Cloud-lens-classic-November",
        r"pwsh -File .\install.ps1",
    ]
    assert "does not publish application code or grant Entra consent" in html
    assert 'href="/login"' in html
    assert "https://cloudlens-web-&lt;suffix&gt;.azurewebsites.net/auth/callback" in html
    template = json.loads((ROOT / "infra" / "azuredeploy.json").read_text(encoding="utf-8"))
    assert template["$schema"].endswith("/subscriptionDeploymentTemplate.json#")
    assert template["resources"]
    assert template["metadata"]["_generator"]["name"] == "bicep"
    assert (ROOT / "web" / "azuredeploy.json").read_bytes() == (
        ROOT / "infra" / "azuredeploy.json").read_bytes()


def test_template_is_public_without_opening_dashboard(client):
    response = client.get("/install/template")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["$schema"].endswith("/subscriptionDeploymentTemplate.json#")


def test_component_colors_use_theme_tokens():
    css = (ROOT / "web" / "assets" / "install.css").read_text(encoding="utf-8")
    components = css[css.index("* {"):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", components)
    assert '"Segoe UI"' in css
    for color in ("#f7f4ef", "#3d3b3a", "#ffffff", "#292929", "#242424",
                  "#dedede", "#b11f4b", "#fd8ea1", "#474747", "#5c5c5c", "#919191"):
        assert color in css
