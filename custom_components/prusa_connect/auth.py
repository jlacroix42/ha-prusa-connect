"""OAuth2 PKCE authentication for Prusa Connect."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from urllib.parse import parse_qs, urlparse

import aiohttp

from .const import (
    AUTH_AUTHORIZE_URL,
    AUTH_CLIENT_ID,
    AUTH_LOGIN_URL,
    AUTH_REDIRECT_URI,
    AUTH_SCOPE,
    AUTH_TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Base authentication error."""


class InvalidCredentialsError(AuthenticationError):
    """Invalid email or password."""


class TotpRequiredError(AuthenticationError):
    """Two-factor authentication TOTP code is required."""

    def __init__(self, session_data: dict) -> None:
        """Initialize with session data needed to continue auth."""
        super().__init__("TOTP code required")
        self.session_data = session_data


class TotpInvalidError(AuthenticationError):
    """Invalid TOTP code."""


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _extract_csrf_token(html: str) -> str | None:
    """Extract CSRF token from login form HTML."""
    match = re.search(
        r'<input[^>]+name=["\']csrfmiddlewaretoken["\'][^>]+value=["\']([^"\']+)',
        html,
    )
    return match.group(1) if match else None


def _extract_totp_token(html: str) -> str | None:
    """Extract CSRF token from TOTP form HTML."""
    return _extract_csrf_token(html)


async def authenticate(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
) -> dict:
    """Authenticate with Prusa Account and return tokens.

    Returns dict with access_token and refresh_token.
    Raises TotpRequiredError if 2FA is enabled.
    Raises InvalidCredentialsError if credentials are wrong.
    """
    code_verifier, code_challenge = _generate_pkce()

    # Step 1: Start OAuth flow - GET authorize URL
    authorize_params = {
        "response_type": "code",
        "client_id": AUTH_CLIENT_ID,
        "redirect_uri": AUTH_REDIRECT_URI,
        "scope": AUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    # Use a cookie jar to maintain session across requests
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as auth_session:
        # GET authorize page - will redirect to login
        async with auth_session.get(
            AUTH_AUTHORIZE_URL,
            params=authorize_params,
            allow_redirects=True,
        ) as resp:
            login_html = await resp.text()
            login_url = str(resp.url)

        csrf_token = _extract_csrf_token(login_html)
        if not csrf_token:
            raise AuthenticationError("Could not find CSRF token in login page")

        # Step 2: POST credentials to login form
        login_data = {
            "csrfmiddlewaretoken": csrf_token,
            "email": email,
            "password": password,
        }

        async with auth_session.post(
            login_url,
            data=login_data,
            headers={"Referer": login_url},
            allow_redirects=False,
        ) as resp:
            if resp.status == 200:
                # Still on login page - check for errors or TOTP
                response_html = await resp.text()
                if "totp" in str(resp.url).lower() or "totp" in response_html.lower():
                    # 2FA is required
                    totp_csrf = _extract_totp_token(response_html)
                    raise TotpRequiredError(
                        {
                            "code_verifier": code_verifier,
                            "totp_url": str(resp.url),
                            "totp_csrf": totp_csrf,
                            "jar": jar,
                        }
                    )
                raise InvalidCredentialsError("Invalid email or password")

            # Follow redirects to capture the authorization code
            location = resp.headers.get("Location", "")
            code = await _follow_redirects_for_code(auth_session, location)

        if not code:
            raise AuthenticationError("Could not obtain authorization code")

        # Step 3: Exchange code for tokens
        return await _exchange_code(session, code, code_verifier)


async def authenticate_totp(
    session: aiohttp.ClientSession,
    totp_code: str,
    session_data: dict,
) -> dict:
    """Complete authentication with TOTP code.

    Returns dict with access_token and refresh_token.
    """
    jar = session_data["jar"]
    code_verifier = session_data["code_verifier"]

    async with aiohttp.ClientSession(cookie_jar=jar) as auth_session:
        totp_data = {
            "csrfmiddlewaretoken": session_data["totp_csrf"],
            "otp_token": totp_code,
        }

        async with auth_session.post(
            session_data["totp_url"],
            data=totp_data,
            headers={"Referer": session_data["totp_url"]},
            allow_redirects=False,
        ) as resp:
            if resp.status == 200:
                raise TotpInvalidError("Invalid TOTP code")

            location = resp.headers.get("Location", "")
            code = await _follow_redirects_for_code(auth_session, location)

        if not code:
            raise AuthenticationError(
                "Could not obtain authorization code after TOTP"
            )

        return await _exchange_code(session, code, code_verifier)


async def refresh_access_token(
    session: aiohttp.ClientSession,
    refresh_token: str,
) -> dict:
    """Refresh an access token using the refresh token.

    Returns dict with new access_token and refresh_token.
    """
    data = {
        "grant_type": "refresh_token",
        "client_id": AUTH_CLIENT_ID,
        "refresh_token": refresh_token,
    }

    async with session.post(AUTH_TOKEN_URL, data=data) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            _LOGGER.error("Token refresh failed (%s): %s", resp.status, error_text)
            raise AuthenticationError(f"Token refresh failed: {resp.status}")

        token_data = await resp.json()
        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", refresh_token),
        }


async def _follow_redirects_for_code(
    session: aiohttp.ClientSession,
    url: str,
) -> str | None:
    """Follow redirect chain until we capture the authorization code.

    After a successful login Prusa Account may answer with an interstitial
    page instead of a redirect, for example ``/passkeys/offer/`` (seen since
    autumn 2026). Such a page keeps the OAuth target in its ``next`` query
    parameter; it is followed once with the now logged-in session.
    """
    max_redirects = 10
    followed_next = False
    for _ in range(max_redirects):
        if not url:
            return None

        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "code" in qs:
            return qs["code"][0]

        # Check if this is a relative URL
        if not parsed.scheme:
            url = f"https://account.prusa3d.com{url}"

        async with session.get(url, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                url = resp.headers.get("Location", "")
                # Check the redirect URL for code parameter
                redirect_parsed = urlparse(url)
                redirect_qs = parse_qs(redirect_parsed.query)
                if "code" in redirect_qs:
                    return redirect_qs["code"][0]
                continue

            # Interstitial page: continue with its "next" target, once.
            next_target = qs.get("next")
            if next_target and not followed_next:
                followed_next = True
                url = next_target[0]
                continue
            return None

    return None


async def _exchange_code(
    session: aiohttp.ClientSession,
    code: str,
    code_verifier: str,
) -> dict:
    """Exchange authorization code for tokens."""
    data = {
        "grant_type": "authorization_code",
        "client_id": AUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": AUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
    }

    async with session.post(AUTH_TOKEN_URL, data=data) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            raise AuthenticationError(f"Token exchange failed ({resp.status}): {error_text}")

        token_data = await resp.json()
        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
        }
