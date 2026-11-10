"""Authentication mode selection must not implicitly request delegated ARM."""
# cloudlens-test: requires-offline
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.entra import EntraSSO


class EntraMode(unittest.TestCase):
    def test_managed_identity_is_default_and_delegation_is_explicit(self):
        for value, expected in [("", False), ("false", False), ("true", True)]:
            with self.subTest(value=value), patch.dict(os.environ, {
                "AUTH_TENANT_ID": "55555555-5555-4555-8555-555555555555",
                "AUTH_CLIENT_ID": "77777777-7777-4777-8777-777777777777",
                "AUTH_DELEGATED_ARM": value,
            }):
                self.assertEqual(EntraSSO().delegated, expected)


if __name__ == "__main__":
    unittest.main()
