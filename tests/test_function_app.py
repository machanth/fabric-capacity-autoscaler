"""Local unit tests. Stubs fabric.functions so the module imports outside Fabric."""
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_fabric = types.ModuleType("fabric")
_fn = types.ModuleType("fabric.functions")


class _UDF:
    def function(self):
        return lambda f: f


_fn.UserDataFunctions = _UDF
_fabric.functions = _fn
sys.modules.setdefault("fabric", _fabric)
sys.modules.setdefault("fabric.functions", _fn)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "functions"))

import function_app as app  # noqa: E402


def _resp(json_data=None):
    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


ARGS = dict(subscription_id="sub", resource_group="rg", capacity_name="cap",
            tenant_id="t", client_id="c", client_secret="s")


class NextSku(unittest.TestCase):
    def test_up_down(self):
        self.assertEqual(app._next_sku("F8", "up"), "F16")
        self.assertEqual(app._next_sku("F16", "down"), "F8")

    def test_floor_ceiling(self):
        self.assertIsNone(app._next_sku("F8", "down"))
        self.assertIsNone(app._next_sku("F256", "up"))

    def test_below_floor_moves_toward_floor(self):
        self.assertEqual(app._next_sku("F2", "up"), "F8")

    def test_out_of_range_never_moves_wrong_way(self):
        self.assertIsNone(app._next_sku("F2", "down"))
        self.assertIsNone(app._next_sku("F512", "up"))
        self.assertEqual(app._next_sku("F512", "down"), "F256")

    def test_unknown(self):
        self.assertIsNone(app._next_sku("P1", "up"))
        self.assertIsNone(app._next_sku("F8", "sideways"))


@patch.object(app, "_headers", return_value={})
class Scale(unittest.TestCase):
    def setUp(self):
        app._last_scale.clear()

    @patch.object(app.requests, "patch")
    @patch.object(app.requests, "get")
    def test_scales_up(self, get, patch_, _h):
        get.return_value = _resp({"properties": {"state": "Active"}, "sku": {"name": "F16"}})
        patch_.return_value = _resp()
        out = app.scale_fabric_capacity(direction="up", **ARGS)
        self.assertEqual(out["status"], "scaled")
        self.assertEqual(out["to_sku"], "F32")
        self.assertEqual(patch_.call_args.kwargs["json"], {"sku": {"name": "F32", "tier": "Fabric"}})

    @patch.object(app.requests, "get")
    def test_cooldown(self, get, _h):
        app._last_scale["cap"] = datetime.now(timezone.utc) - timedelta(minutes=1)
        out = app.scale_fabric_capacity(direction="up", **ARGS)
        self.assertEqual(out["reason"], "cooldown")
        get.assert_not_called()

    @patch.object(app.requests, "get")
    def test_paused_down_noop(self, get, _h):
        get.return_value = _resp({"properties": {"state": "Paused"}, "sku": {"name": "F16"}})
        self.assertEqual(app.scale_fabric_capacity(direction="down", **ARGS)["reason"], "paused")

    @patch.object(app.requests, "get")
    def test_busy_noop(self, get, _h):
        get.return_value = _resp({"properties": {"state": "Updating"}, "sku": {"name": "F16"}})
        self.assertEqual(app.scale_fabric_capacity(direction="up", **ARGS)["status"], "noop")

    def test_bad_direction(self, _h):
        self.assertEqual(app.scale_fabric_capacity(direction="x", **ARGS)["status"], "error")


if __name__ == "__main__":
    unittest.main()
