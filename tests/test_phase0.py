from __future__ import annotations

import inspect
import logging

from aambridge.logging_config import configure_logging
from aambridge.settings import Settings
import scraper_common


def test_driver_configuration_has_no_evasion_options() -> None:
    source = inspect.getsource(scraper_common.create_driver)

    assert "excludeSwitches" not in source
    assert "useAutomationExtension" not in source
    assert "navigator.webdriver" not in source
    assert "user-agent" not in source


def test_settings_read_linkedin_credentials_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_EMAIL", "user@example.com")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "local-secret")

    settings = Settings(_env_file=None)

    assert settings.linkedin_email == "user@example.com"
    assert settings.linkedin_password == "local-secret"
    assert settings.has_linkedin_credentials


def test_settings_require_both_credentials(monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_EMAIL", "user@example.com")
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)

    assert not Settings(_env_file=None).has_linkedin_credentials


def test_logging_writes_to_rotating_file(tmp_path) -> None:
    settings = Settings(log_dir=tmp_path)
    configure_logging(settings)

    logging.getLogger("test").info("phase-zero-log-message")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "phase-zero-log-message" in (
        tmp_path / "aambridge.log"
    ).read_text(encoding="utf-8")


def test_missing_credentials_fall_back_to_manual_login(monkeypatch) -> None:
    class Driver:
        current_url = "https://www.linkedin.com/login"
        visited_url = ""

        def get(self, url: str) -> None:
            self.visited_url = url

    messages: list[str] = []
    driver = Driver()
    monkeypatch.delenv("LINKEDIN_EMAIL", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)
    monkeypatch.setattr(
        scraper_common,
        "Settings",
        lambda **kwargs: Settings(_env_file=None, **kwargs),
    )
    monkeypatch.setattr(
        scraper_common,
        "_finish_login_manually",
        lambda _driver, message: messages.append(message),
    )

    scraper_common.automatic_login(driver)

    assert driver.visited_url == "https://www.linkedin.com/login"
    assert "not both configured" in messages[0]

