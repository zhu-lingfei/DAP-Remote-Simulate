import contextlib
import unittest
from unittest.mock import Mock, patch

from dapremote.ui_dialogs import centered_rect, _centered


class DialogPlacementTests(unittest.TestCase):
    def test_centers_in_workarea_without_covering_taskbar(self):
        self.assertEqual(centered_rect((0, 0, 1920, 1040), 900, 620), (900, 620, 510, 210))

    def test_handles_negative_coordinates_on_a_secondary_monitor(self):
        self.assertEqual(centered_rect((-1920, 100, 0, 1180), 900, 620), (900, 620, -1410, 330))

    def test_oversized_dialog_is_constrained_to_available_screen(self):
        self.assertEqual(centered_rect((100, 40, 900, 640), 1200, 900), (800, 600, 100, 40))

    def test_dialog_proxy_preserves_result_options_and_owner(self):
        native = Mock(return_value=False)
        parent = object()
        with patch("dapremote.ui_dialogs.native_dialog_centering", return_value=contextlib.nullcontext()) as positioning:
            result = _centered(native)("覆盖？", "确认", parent=parent, default="no")
        self.assertFalse(result)
        positioning.assert_called_once_with(parent)
        native.assert_called_once_with("覆盖？", "确认", parent=parent, default="no")

    def test_dialog_exception_closes_positioning_scope(self):
        exited = []
        @contextlib.contextmanager
        def scope(_):
            try:
                yield
            finally:
                exited.append(True)
        with patch("dapremote.ui_dialogs.native_dialog_centering", side_effect=scope):
            with self.assertRaises(RuntimeError):
                _centered(Mock(side_effect=RuntimeError("cancelled")))(parent=object())
        self.assertEqual(exited, [True])
