import tempfile
import unittest
from pathlib import Path

from utils.settings import ConfigurationError, load_settings


VALID_CONFIG = """
[iaaa]
username = student
password = secret

[epe]
phone = 13800000000

[recognize]
method = ttshitu

[recognize:ttshitu]
username = captcha-user
password = captcha-pass

[notify]
method = none
"""


class SettingsTests(unittest.TestCase):
    def _write_config(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.ini"
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_only_selected_adapter_settings(self):
        settings = load_settings(self._write_config(VALID_CONFIG))

        self.assertEqual(settings.iaaa.username, "student")
        self.assertEqual(settings.epe.phone, "13800000000")
        self.assertEqual(settings.recognition.method, "ttshitu")
        self.assertEqual(settings.recognition.username, "captcha-user")
        self.assertEqual(settings.notification.method, "none")

    def test_reports_missing_required_value_with_section_and_option(self):
        config = VALID_CONFIG.replace("password = captcha-pass", "")

        with self.assertRaisesRegex(
            ConfigurationError,
            r"\[recognize:ttshitu\] password",
        ):
            load_settings(self._write_config(config))

    def test_reports_missing_config_file_before_startup(self):
        missing = Path(tempfile.gettempdir()) / "missing-pkuautovenues-config.ini"
        missing.unlink(missing_ok=True)

        with self.assertRaisesRegex(ConfigurationError, "Config file not found"):
            load_settings(missing)


if __name__ == "__main__":
    unittest.main()
