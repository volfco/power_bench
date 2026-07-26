import unittest
import tempfile
from pathlib import Path

from host_metadata import classify_host, enrich_host_metadata, hardware_generation
from database import Database


class HostMetadataTests(unittest.TestCase):
    def test_chassis_type_takes_precedence(self):
        self.assertEqual(classify_host(10, system_model="Generic"), "Laptop")
        self.assertEqual(classify_host(23, system_model="Generic"), "Server")
        self.assertEqual(classify_host(35, system_model="Generic"), "Mini PC")

    def test_identity_fallbacks_cover_requested_groups(self):
        self.assertEqual(classify_host(system_model="ThinkPad T14"), "Laptop")
        self.assertEqual(classify_host(system_model="PowerEdge R740"), "Server")
        self.assertEqual(classify_host(system_model="Intel NUC 13 Pro"), "Mini PC")

    def test_generation_is_extracted_from_common_cpu_names(self):
        self.assertEqual(
            hardware_generation("Intel(R) Core(TM) i7-8650U CPU @ 1.90GHz"),
            "Intel Core 8th Gen",
        )
        self.assertEqual(
            hardware_generation("AMD Ryzen 7 7840U with Radeon Graphics"),
            "AMD Ryzen 7000 series",
        )
        self.assertEqual(
            hardware_generation("Intel(R) Processor N100"),
            "Intel Alder Lake-N (12th Gen)",
        )

    def test_explicit_dimensions_are_preserved(self):
        values = enrich_host_metadata(
            {
                "cpu_model": "AMD Ryzen 7 7840U",
                "host_group": "Server",
                "hardware_generation": "Lab generation A",
            }
        )
        self.assertEqual(values["host_group"], "Server")
        self.assertEqual(values["hardware_generation"], "Lab generation A")

    def test_database_migration_backfills_legacy_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.duckdb"
            with Database(str(path)) as database:
                run_id = database.create_run(
                    cpu_model="Intel(R) Core(TM) i7-8650U CPU @ 1.90GHz",
                    applied_config='{"system_model":"ThinkPad T480","chassis_type":10}',
                )
            with Database(str(path)) as database:
                row = database.query(
                    "SELECT host_group, hardware_generation, system_model, chassis_type "
                    "FROM runs WHERE run_id = ?",
                    [run_id],
                )[0]
        self.assertEqual(
            row, ("Laptop", "Intel Core 8th Gen", "ThinkPad T480", 10)
        )


if __name__ == "__main__":
    unittest.main()
