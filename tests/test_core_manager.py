import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("xray_core_manager", ROOT / "xray-core-manager.py")
assert SPEC and SPEC.loader
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


class CoreManagerTests(unittest.TestCase):
    def test_parse_xray_version_and_marker(self):
        statement = "Xray 26.6.27 (Xray, Penetrates Everything.) xhttp-cleaner-v3-26.6.27 (go1.26 linux/amd64)"
        self.assertEqual(manager.parse_version(statement), "26.6.27")
        self.assertIn(manager.PATCH_ID, statement)

    def test_parse_version_rejects_unexpected_output(self):
        with self.assertRaises(manager.CoreManagerError):
            manager.parse_version("not xray")

    def test_architecture_mapping_is_explicit(self):
        self.assertEqual(manager.normalize_arch("x86_64"), ("amd64", "amd64"))
        self.assertEqual(manager.normalize_arch("aarch64"), ("arm64", "arm64"))
        with self.assertRaises(manager.CoreManagerError):
            manager.normalize_arch("mips")

    def test_container_name_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({"container": "remnanode-1"}), encoding="utf-8")
            self.assertEqual(manager.load_container(config), "remnanode-1")
            config.write_text(json.dumps({"container": "bad;name"}), encoding="utf-8")
            with self.assertRaises(manager.CoreManagerError):
                manager.load_container(config)

    def test_artifacts_are_version_and_architecture_scoped(self):
        info = {"version": "26.6.27", "arch": "amd64"}
        path = manager.artifact_path(info)
        self.assertIn("26.6.27-amd64", str(path))
        self.assertIn(manager.PATCH_ID, str(path))

    def test_settings_fingerprint_ignores_runtime_state(self):
        base = [{
            "Config": {"Env": ["SECRET=value"], "Image": "remnawave/node:latest"},
            "HostConfig": {"RestartPolicy": {"Name": "always"}, "PortBindings": {}},
            "Mounts": [{"Source": "/opt/rnode", "Destination": "/var/lib/rnode"}],
            "State": {"StartedAt": "before"},
            "NetworkSettings": {"Networks": {"bridge": {
                "Aliases": ["remnanode"], "Links": None, "IPAMConfig": None,
                "DriverOpts": None, "NetworkID": "network", "IPAddress": "172.17.0.2",
            }}},
        }]
        changed_runtime = json.loads(json.dumps(base))
        changed_runtime[0]["State"]["StartedAt"] = "after"
        changed_runtime[0]["NetworkSettings"]["Networks"]["bridge"]["IPAddress"] = "172.17.0.3"
        self.assertEqual(
            manager.preserved_container_settings(json.dumps(base)),
            manager.preserved_container_settings(json.dumps(changed_runtime)),
        )


if __name__ == "__main__":
    unittest.main()
