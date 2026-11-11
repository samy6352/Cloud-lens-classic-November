import hashlib
import json
import zipfile

import pytest

from tools import customer_release


@pytest.mark.parametrize("change", ["unchanged", "changed", "missing"])
def test_release_currency_rejects_stale_sources_and_missing_modules(tmp_path, monkeypatch, change):
    root = tmp_path / "source"
    (root / "app").mkdir(parents=True)
    content = b'"""Current runtime."""\n'
    (root / "app" / "main.py").write_bytes(content)
    monkeypatch.setattr(customer_release, "ROOT", root)
    output = tmp_path / "customer.zip"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("app/main.py", content)
        archive.writestr("customer-manifest.json", json.dumps({
            "files": [{"path": "app/main.py", "sha256": hashlib.sha256(content).hexdigest()}],
        }))
    if change == "changed":
        (root / "app" / "main.py").write_bytes(b'"""Updated runtime."""\n')
        with pytest.raises(ValueError, match="stale"):
            customer_release.verify_current(output)
    elif change == "missing":
        (root / "app" / "advisor_score.py").write_bytes(content)
        with pytest.raises(ValueError, match="missing"):
            customer_release.verify_current(output)
    else:
        customer_release.verify_current(output)
