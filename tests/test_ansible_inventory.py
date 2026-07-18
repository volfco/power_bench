import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ansible_inventory import InventoryHostError, resolve_inventory_host


class InventoryHostTests(unittest.TestCase):
    def test_exact_inventory_alias_resolves_connection_details(self):
        inventory = {
            "_meta": {
                "hostvars": {
                    "node2": {
                        "ansible_host": "192.0.2.20",
                        "ansible_user": "bench",
                        "ansible_private_key_file": "/keys/bench",
                        "power_bench_sweep_profile": "amd",
                    }
                }
            }
        }
        with patch(
            "ansible_inventory.subprocess.run",
            return_value=SimpleNamespace(stdout=json.dumps(inventory)),
        ):
            host = resolve_inventory_host("node2", "ansible/hosts")

        self.assertEqual(host.name, "node2")
        self.assertEqual(host.address, "192.0.2.20")
        self.assertEqual(host.user, "bench")
        self.assertEqual(host.private_key, "/keys/bench")
        self.assertEqual(host.sweep, "amd")

    def test_literal_ip_is_rejected_before_inventory_lookup(self):
        with patch("ansible_inventory.subprocess.run") as run:
            with self.assertRaisesRegex(InventoryHostError, "not an IP address"):
                resolve_inventory_host("192.0.2.20", "ansible/hosts")

        run.assert_not_called()

    def test_unknown_alias_is_rejected(self):
        inventory = {"_meta": {"hostvars": {"node2": {}}}}
        with patch(
            "ansible_inventory.subprocess.run",
            return_value=SimpleNamespace(stdout=json.dumps(inventory)),
        ):
            with self.assertRaisesRegex(InventoryHostError, "available hosts: node2"):
                resolve_inventory_host("node9", "ansible/hosts")


if __name__ == "__main__":
    unittest.main()
