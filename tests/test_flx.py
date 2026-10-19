"""Regression: FLX can be stored as a file but cannot be selected as an algorithm."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_artifacts import ArtifactTests as _Fixture
from dapremote.firmware import artifact_kind, artifact_path, import_artifact
from dapremote.gui import App
from dapremote.client import RemoteError
from dapremote.protocol import Op, Status


class FlmOnlyTests(unittest.TestCase):
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown

    def test_algorithm_kind_and_path_allow_only_flm_case_insensitively(self):
        for filename in ("chip.FLM", "chip.flm", "chip.fLm"):
            self.assertEqual(artifact_kind(filename), "flm")
            self.assertEqual(artifact_path("/FLM/" + filename, "flm"), "/FLM/" + filename)
        for filename in ("chip.FLX", "chip.flx", "notes.txt"):
            self.assertIsNone(artifact_kind(filename))
            with self.assertRaises(ValueError):
                artifact_path("/FLM/" + filename, "flm")

    def test_import_rejects_flx_as_an_algorithm(self):
        source = self.base / "external.FLX"
        source.write_bytes(self.local_flm.read_bytes())
        with self.assertRaises(ValueError):
            import_artifact(self.client, source, "flm", self.cache)

    def test_client_refuses_flx_before_creating_a_job(self):
        firmware = import_artifact(self.client, self.local_hex, "hex", self.cache)
        with self.assertRaisesRegex(ValueError, "FLM"):
            self.client.start_flash(firmware["path"], "flx-rejected", flm_path="/FLM/external.FLX")
        self.assertFalse(self.server.device.jobs)

    def test_dropdown_only_lists_flm_from_the_designated_root(self):
        entries = [dict(name=name, path="/FLM/" + name, version="a" * 64, is_dir=False)
                   for name in ("AM29x800BBx2.FLX", "AM29x800DB.FLX", "chip.FLM", "notes.txt")]
        entries.append(dict(name="sub.flm", path="/FLM/sub/sub.flm", version="b" * 64, is_dir=False))
        view = SimpleNamespace(selected_flm=None, flm_combo=Mock(), flm_choice=Mock(), flm_filename=Mock(),
                               flm_list_hint=Mock(), _update_flash_button=Mock())
        App._apply_flm_files(view, entries)
        self.assertEqual(set(view.flm_files), {"chip.FLM"})
        self.assertIn("1 个算法", view.flm_list_hint.set.call_args.args[0])
        self.assertIn("已过滤 3 个", view.flm_list_hint.set.call_args.args[0])

    def test_device_rejects_flx_even_when_bypassing_client_checks(self):
        firmware = import_artifact(self.client, self.local_hex, "hex", self.cache)
        self.client.mkdir("/FLM")
        self.client.upload(self.local_flm, "/FLM/external.FLX")
        info = self.client.stat("/FLM/external.FLX")
        with self.assertRaises(RemoteError) as error:
            self.client.call(Op.FLASH_CHECK, path=firmware["path"], version=firmware["version"],
                             flm_path=info["path"], flm_version=info["version"])
        self.assertEqual(error.exception.status, Status.UNSUPPORTED)
        self.assertIn("FLX", str(error.exception))
        self.assertFalse(self.server.device.jobs)


del _Fixture
