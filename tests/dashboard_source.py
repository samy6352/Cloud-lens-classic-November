"""Read the shell and its real lazy modules for cross-dashboard structural contracts."""
from pathlib import Path


def read_dashboard():
    assets = Path(__file__).resolve().parents[1] / "web" / "assets"
    paths = [assets / "dashboard.js", *sorted(assets.glob("dashboard-*.js"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)
