"""Interactive native authorization in the user's browser, with PKCE callback capture."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Playwright, Response, async_playwright

from .client import AuthenticationRequired, CfeError
from .mobile import HOST, REDIRECT_URI, Authorization, MobileSession
from .storage import data_dir


class MobileBrowserLogin:
    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.authorization: Authorization | None = None
        self.callback: str | None = None
        self.ready = asyncio.Event()

    async def _response(self, response: Response) -> None:
        parsed = urlsplit(response.url)
        if self.callback or (parsed.scheme, parsed.netloc) != ("https", HOST):
            return
        try:
            location = (await response.all_headers()).get("location", "")
        except Exception:
            return
        if location.startswith(REDIRECT_URI):
            self.callback = location
            self.ready.set()

    async def start(self) -> dict[str, Any]:
        if self.page and not self.page.is_closed():
            return {
                "status": "browser_open",
                "message": "Terminer la connexion mobile dans Chrome.",
            }
        await self.close()
        self.authorization = Authorization()
        self.playwright = await async_playwright().start()
        channel = os.environ.get("CFE_BROWSER_CHANNEL", "chrome")
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(data_dir() / "mobile-browser"),
                channel=None if channel == "chromium" else channel,
                headless=False,
            )
            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            self.page.on("response", self._response)
            await self.page.bring_to_front()
            try:
                await self.page.goto(self.authorization.url(), wait_until="domcontentloaded")
            except Exception:
                if not self.callback:
                    raise
            return {
                "status": "login_required",
                "message": "Saisir les identifiants et le code e-mail dans la fenêtre CFE, "
                "puis appeler cfe_mobile_finish_login.",
            }
        except Exception:
            await self.close()
            raise CfeError("Impossible d'ouvrir la connexion mobile CFE.") from None

    async def finish(self, verification_code: str | None = None) -> dict[str, Any]:
        if not self.authorization or not self.page:
            raise AuthenticationRequired("Ouvrir la connexion avec cfe_mobile_start_login.")
        if verification_code is not None and not self.callback:
            if not verification_code.isascii() or not verification_code.isdigit():
                raise CfeError("Format du code e-mail invalide.")
            if not 4 <= len(verification_code) <= 10:
                raise CfeError("Format du code e-mail invalide.")
            if urlsplit(self.page.url).hostname != HOST:
                raise CfeError("Le navigateur n'attend pas un code CFE.")
            try:
                field = self.page.locator("#readOnlyEmail_ver_input")
                if not await field.is_visible():
                    raise AuthenticationRequired("Le champ du code e-mail n'est pas visible.")
                await field.fill(verification_code)
                await self.page.locator("#readOnlyEmail_ver_but_verify").click()
                await asyncio.wait_for(self.ready.wait(), timeout=10)
            except TimeoutError:
                pass
            except CfeError:
                raise
            except Exception:
                raise CfeError("Impossible de saisir le code dans la fenêtre CFE.") from None
        if not self.callback:
            raise AuthenticationRequired("Terminer la connexion dans la fenêtre CFE.")
        try:
            async with MobileSession() as session:
                return await session.exchange(self.authorization, self.callback)
        finally:
            await self.close()

    async def close(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        self.page = None
        self.authorization = None
        self.callback = None
        self.ready.clear()
