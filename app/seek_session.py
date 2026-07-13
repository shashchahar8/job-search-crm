import logging
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from app.collectors.seek import (
    SEEK_BASE_URL,
    detect_genuine_access_challenge,
    detect_signed_in_session,
)
from app.config import Settings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionReadiness:
    is_open: bool
    is_signed_in: bool
    message: str
    current_url: str | None = None


class SeekSessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright = None
        self._context = None
        self._page: Page | None = None

    def is_profile_busy(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    def open_prepare_browser(self) -> SessionReadiness:
        if self._page and not self._page.is_closed():
            self._page.bring_to_front()
            return self.readiness()

        self.settings.playwright_profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.playwright_profile_dir),
            headless=False,
            args=[
                "--disable-save-password-bubble",
                "--disable-features=PasswordManagerOnboarding",
            ],
        )
        self._page = self._context.new_page()
        self._page.goto(SEEK_BASE_URL, wait_until="domcontentloaded", timeout=45_000)
        self._page.wait_for_timeout(1000)
        return self.readiness()

    def readiness(self) -> SessionReadiness:
        if self._page is None or self._page.is_closed():
            self.close()
            return SessionReadiness(
                is_open=False,
                is_signed_in=False,
                message="SEEK preparation browser is not open.",
            )
        try:
            html = self._page.content()
            current_url = self._page.url
        except PlaywrightError:
            self.close()
            return SessionReadiness(
                is_open=False,
                is_signed_in=False,
                message="SEEK preparation browser was closed. The profile is released.",
            )
        challenge = detect_genuine_access_challenge(html, current_url)
        if challenge:
            return SessionReadiness(
                is_open=True,
                is_signed_in=False,
                message=f"{challenge}. Complete it in the visible browser, then confirm again.",
                current_url=current_url,
            )
        signed_in = detect_signed_in_session(html)
        if signed_in:
            return SessionReadiness(
                is_open=True,
                is_signed_in=True,
                message="Signed-in SEEK session detected in the persistent browser profile.",
                current_url=current_url,
            )
        return SessionReadiness(
            is_open=True,
            is_signed_in=False,
            message=(
                "SEEK is open. Sign in manually if needed, then click I finished signing in. "
                "If search results are available without signing in, runs may continue without it."
            ),
            current_url=current_url,
        )

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        except PlaywrightError:
            LOGGER.exception("seek_prepare_context_close_failed")
        finally:
            self._context = None
            self._page = None
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
