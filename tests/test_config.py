"""Regression checks for the offline CLI's grouped proxy configuration."""

import json
from pathlib import Path
import tempfile
import unittest

from manage import resolve_proxy
from siwc import PLUGIN_NAME
from siwc.errors import SIWCError


class CLIProxyConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_grouped_proxy_file_and_cli_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "plugin_data" / PLUGIN_NAME
            config_file = root / "config" / f"{PLUGIN_NAME}_config.json"
            config_file.parent.mkdir()
            config_file.write_text(json.dumps({"network": {"proxy": "http://127.0.0.1:3210"}}), encoding="utf-8-sig")
            proxy, source = await resolve_proxy(directory, None)
            self.assertEqual(proxy, "http://127.0.0.1:3210")
            self.assertEqual(source, "插件设置")
            proxy, source = await resolve_proxy(directory, "")
            self.assertEqual(proxy, "")
            self.assertEqual(source, "命令行")

    async def test_invalid_grouped_proxy_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config" / f"{PLUGIN_NAME}_config.json"
            config_file.parent.mkdir()
            config_file.write_text(json.dumps({"network": {"proxy": False}}), encoding="utf-8")
            with self.assertRaises(SIWCError):
                await resolve_proxy(root / "plugin_data" / PLUGIN_NAME, None)


if __name__ == "__main__":
    unittest.main()
