"""Interactive login; the normal CFE browser flow handles credentials and MFA."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .client import ORIGIN, AuthenticationRequired, CfeError, is_authenticated_html
from .storage import data_dir, write_private_json


async def save_session(context: BrowserContext, page: Page) -> dict[str, Any]:
    if urlsplit(page.url).netloc != "monespaceperso.cfe.fr":
        raise AuthenticationRequired("Terminer la connexion dans la fenêtre CFE.")
    if not is_authenticated_html(await page.content()):
        raise AuthenticationRequired("La connexion CFE n'est pas encore terminée.")
    state = await context.storage_state()
    cookies = [c for c in state["cookies"] if c["domain"].lstrip(".") == "monespaceperso.cfe.fr"]
    if not any(c["name"] == ".AspNet.ApplicationCookie" for c in cookies):
        raise AuthenticationRequired("Cookie de connexion CFE absent.")
    write_private_json(data_dir() / "browser-state.json", {"cookies": cookies, "origins": []})
    return {"authenticated": True, "session_saved": True}


async def import_browser(cdp_url: str) -> dict[str, Any]:
    parsed = urlsplit(cdp_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CfeError("L'import de session nécessite un navigateur local.")
    if parsed.scheme not in {"http", "ws"}:
        raise CfeError("Adresse CDP locale invalide.")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        for context in browser.contexts:
            for page in context.pages:
                if urlsplit(page.url).netloc == "monespaceperso.cfe.fr":
                    return await save_session(context, page)
    raise AuthenticationRequired("Aucun onglet CFE connecté trouvé.")


class BrowserLogin:
    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self) -> dict[str, Any]:
        if self.page and not self.page.is_closed():
            return {"status": "browser_open", "message": "Terminer la connexion dans Chrome."}
        self.playwright = await async_playwright().start()
        channel = os.environ.get("CFE_BROWSER_CHANNEL", "chrome")
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(data_dir() / "login-browser"),
                channel=None if channel == "chromium" else channel,
                headless=False,
            )
            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            await self.page.goto(ORIGIN, wait_until="domcontentloaded", timeout=60000)
            if is_authenticated_html(await self.page.content()):
                result = await save_session(self.context, self.page)
                await self.close()
                return result
            link = self.page.locator('a[href*="/Account/Login/ExternalLogin"]').first
            href = await link.get_attribute("href")
            if not href:
                raise CfeError("Lien de connexion CFE introuvable.")
            from .client import portal_url

            await self.page.goto(portal_url(href), wait_until="domcontentloaded", timeout=60000)
            return {
                "status": "login_required",
                "message": "Saisir les identifiants et le code e-mail dans la fenêtre Chrome, "
                "puis appeler cfe_finish_login.",
            }
        except Exception:
            await self.close()
            raise CfeError("Impossible d'ouvrir la connexion CFE dans Chrome.") from None

    async def finish(self, verification_code: str | None = None) -> dict[str, Any]:
        if not self.page or not self.context or self.page.is_closed():
            raise AuthenticationRequired("Ouvrir la connexion avec cfe_start_login.")
        if verification_code is not None:
            if not verification_code.isdigit() or not 4 <= len(verification_code) <= 10:
                raise CfeError("Format du code de vérification invalide.")
            if urlsplit(self.page.url).hostname != "cfeb2cprod.b2clogin.com":
                raise CfeError("Le navigateur n'attend pas un code CFE.")
            field = self.page.locator("#readOnlyEmail_ver_input")
            if not await field.is_visible():
                raise CfeError("Le champ du code e-mail n'est pas visible.")
            await field.fill(verification_code)
            await self.page.locator("#readOnlyEmail_ver_but_verify").click()
            try:
                await self.page.wait_for_url(f"{ORIGIN}/**", timeout=10000)
            except Exception:
                raise AuthenticationRequired(
                    "Vérifier le message de validation dans Chrome, puis réessayer."
                ) from None
        result = await save_session(self.context, self.page)
        await self.close()
        return result

    async def close(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        self.page = None
