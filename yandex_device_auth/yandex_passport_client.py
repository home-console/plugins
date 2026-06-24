"""
Yandex Passport Client (Home Assistant PWL style).

Supports PWL (Passport Web Login) QR-based auth:
- QR code generation via GET https://passport.yandex.ru/pwl-yandex/auth/add
- Auto-approve when user scans QR in app
- Extracts session cookies after confirmation

Reference: https://github.com/AlexxIT/YandexGlagol/blob/main/yandex_glagol/yandex_session.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("yandex_device_auth")


class DeviceAuthSession:
    """Persistent session for PWL QR auth lifecycle.
    
    Holds:
    - client_session: aiohttp.ClientSession (persistent cookie jar)
    - pwl_params: PWL flow params (retpath)
    - created_at: timestamp for timeout tracking
    
    How it works:
    1. GET to PWL URL initiates session
    2. User scans QR in app
    3. App auto-confirms
    4. Cookies (Session_id, yandexuid) auto-populate jar
    5. We detect and extract
    """

    def __init__(self, session: aiohttp.ClientSession):
        """Initialize persistent session."""
        self.client_session = session
        self.pwl_params: Dict[str, str] = {}
        self.device_id: Optional[str] = None
        self.created_at = time.time()

    async def close(self):
        """Close the session."""
        if self.client_session:
            await self.client_session.close()

    def is_expired(self, timeout_sec: int = 600) -> bool:
        """Check if session is expired (default 10 minutes)."""
        return (time.time() - self.created_at) > timeout_sec


class YandexPassportClient:
    """Yandex Passport PWL (Password-Less) QR auth client.
    
    Implements simple PWL QR flow:
    1. GET /pwl-yandex/auth/add?retpath=... (initiates session)
    2. User scans QR in Yandex app (auto-approve)
    3. Cookies appear in session jar (Session_id, yandexuid)
    4. Exchange cookies for x_token
    
    NO bootstrap, NO HTML parsing, NO OAuth.
    """

    CLIENT_ID = "c0ebe342af7d48fbbbfcf2d2eedb8f9e"
    CLIENT_SECRET = "ad0a908f0aa341a182a37ecd75bc319e"

    def __init__(self):
        """Initialize client."""
        import os
        self.client_secret = (
            os.environ.get("YANDEX_CLIENT_SECRET") or self.CLIENT_SECRET
        )

    async def get_qr_url(self, auth_session: DeviceAuthSession) -> Optional[Dict[str, str]]:
        """Get PWL QR URL - simple GET request to Yandex PWL page.
        
        CRITICAL: Must start polling immediately to signal Yandex that login is monitored.
        Yandex considers flow "abandoned" if no polling happens within seconds.
        
        Flow:
        1. PRE-WARM: Initialize session with GET to passport.yandex.ru
        2. Make GET request to PWL page (this initiates session)
        3. START POLLING IMMEDIATELY (before returning to user)
        4. Polling continues until user confirms (302 redirect)
        5. FINALIZE with GET to retpath
        6. Return QR URL for user to scan
        
        NO bootstrap, NO HTML parsing, NO OAuth.
        
        Args:
            auth_session: DeviceAuthSession with persistent client_session
        
        Returns:
            {
                "qr_url": str,  # https://passport.yandex.ru/pwl-yandex/auth/add?retpath=...
                "status": str,  # "pending"
            } or None if failed
        """
        session = auth_session.client_session
        retpath = "https://passport.yandex.ru/pwl-yandex/am/push/qrsecure"
        qr_url = f"https://passport.yandex.ru/pwl-yandex/auth/add?retpath={urllib.parse.quote(retpath, safe='')}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        try:
            # PRE-WARM: Initialize session
            logger.info("[Yandex] PWL: Pre-warming session...")
            try:
                async with session.get("https://passport.yandex.ru/", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    _ = await resp.text()
                    logger.debug(f"[Yandex] Pre-warm response: {resp.status}")
            except Exception as e:
                logger.warning(f"[Yandex] Pre-warm request failed (non-critical): {e}")
            
            logger.info(f"[Yandex] PWL: Initial GET to create QR...")
            logger.info(f"[Yandex] Request URL: {qr_url}")
            
            # GET QR page - WITHOUT auto-redirects
            async with session.get(
                qr_url,
                headers=headers,
                allow_redirects=False,
            ) as resp:
                if resp.status not in [200, 302]:
                    body = await resp.text()
                    logger.error(f"[Yandex] PWL GET failed: {resp.status}")
                    logger.error(f"[Yandex] Response body: {body[:300]}")
                    logger.error(f"[Yandex] Response headers: {dict(resp.headers)}")
                    return None
                
                html = await resp.text()
                logger.info(f"[Yandex] ✓ QR page response: {resp.status}, HTML length: {len(html)}")
                logger.debug(f"[Yandex] Set-Cookie in response: {resp.headers.getall('Set-Cookie', [])}")
                logger.debug(f"[Yandex] Cookies in jar after GET: {[(c.key, c.value[:20]) for c in session.cookie_jar]}")
            
            # Store for polling
            auth_session.pwl_params = {
                "qr_url": qr_url,
                "retpath": retpath,
                "created_at": time.time(),
            }
            
            logger.info(f"[Yandex] QR URL: {qr_url[:80]}...")
            logger.info("[Yandex] Starting immediate polling (Yandex requires active monitoring)...")
            
            # START POLLING IMMEDIATELY IN BACKGROUND
            # This signals to Yandex that login is being actively monitored
            asyncio.create_task(self._continuous_poll(auth_session))
            
            return {
                "qr_url": qr_url,
                "status": "pending",
            }

        except Exception as e:
            logger.error(f"[Yandex] Error in get_qr_url: {e}", exc_info=True)
            return None

    async def _continuous_poll(self, auth_session: DeviceAuthSession, timeout_sec: int = 600):
        """Continuous polling for PWL QR approval.
        
        Success = 302 redirect (user approved in app)
        
        Does NOT wait for Session_id (Yandex doesn't issue it server-side for PWL QR).
        Simply detects approval via 302 and marks session as approved.
        """
        if not auth_session.pwl_params:
            logger.error("[Yandex] No PWL params for polling")
            return
        
        session = auth_session.client_session
        qr_url = auth_session.pwl_params.get("qr_url")
        start_time = time.time()
        poll_count = 0

        logger.info("[Yandex] 🔄 Starting PWL polling (waiting for user approval)...")

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout_sec:
                logger.error(f"[Yandex] PWL timeout after {timeout_sec}s")
                auth_session.pwl_params["approved"] = False
                return

            poll_count += 1

            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                async with session.get(qr_url, allow_redirects=False, headers=headers) as resp:
                    status_code = resp.status
                    
                    logger.debug(f"[Yandex] Poll #{poll_count} at {elapsed:.1f}s: HTTP {status_code}")
                    
                    # Check for 302 = user approved
                    if status_code == 302:
                        logger.info(f"[Yandex] ✅ USER APPROVED! Got 302 redirect")
                        auth_session.pwl_params["approved"] = True
                        return
                    
                    # Read response to keep session alive
                    _ = await resp.text()

                # Poll every 1 second
                await asyncio.sleep(1)

            except asyncio.CancelledError:
                logger.debug("[Yandex] Polling cancelled")
                return
            except Exception as e:
                logger.warning(f"[Yandex] Poll error: {e}")
                await asyncio.sleep(1)

    async def poll_qr_until_approved(self, auth_session: DeviceAuthSession, timeout_sec: int = 600) -> bool:
        """DEPRECATED: Use _continuous_poll instead (runs automatically from get_qr_url)."""
        logger.warning("[Yandex] poll_qr_until_approved is deprecated, use _continuous_poll instead")
        await self._continuous_poll(auth_session, timeout_sec)

    async def check_qr_status(self, auth_session: DeviceAuthSession) -> Optional[Dict[str, Any]]:
        """Check if user approved QR and get x_token via device auth flow.
        
        Args:
            auth_session: DeviceAuthSession with persistent client_session
        
        Returns:
            Auth result dict with x_token if approved, None if not yet
        """
        if not auth_session.pwl_params:
            logger.warning("[Yandex] No PWL params, call get_qr_url first")
            return None

        try:
            # Check if polling detected approval (302 redirect)
            approved = auth_session.pwl_params.get("approved", False)
            
            if not approved:
                logger.debug("[Yandex] QR not approved yet")
                return None
            
            logger.info("[Yandex] ✅ QR approved - getting x_token via magic flow...")
            
            # Get x_token via magic flow (correct way for PWL QR)
            x_token = await self._get_x_token_magic()
            if not x_token:
                logger.error("[Yandex] Failed to get x_token")
                return None
            
            # Validate x_token and get account info
            result = await self._validate_x_token(auth_session.client_session, x_token)
            if result:
                logger.info(f"[Yandex] ✅ Successfully authenticated as {result.get('display_login', 'unknown')}")
                return result
            
            return None

        except Exception as e:
            logger.error(f"[Yandex] Error checking QR status: {e}")
            return None

    async def _get_x_token_magic(self) -> Optional[str]:
        """Get x_token via magic flow.
        
        This is the correct endpoint for PWL QR server-side auth.
        """
        try:
            url = "https://mobileproxy.passport.yandex.net/1/bundle/auth/x_token/"
            
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Ya-Consumer-Client-Id": self.CLIENT_ID,
                "Ya-Consumer-Client-Secret": self.CLIENT_SECRET,
            }
            
            data = {
                "type": "magic",
                "retpath": "https://yandex.ru",
            }
            
            logger.debug(f"[Yandex] Getting x_token via magic flow...")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"[Yandex] Magic flow failed: {resp.status}")
                        logger.error(f"[Yandex] Response: {body[:200]}")
                        return None
                    
                    response_data = await resp.json()
                    
                    if response_data.get("status") != "ok":
                        logger.error(f"[Yandex] Magic flow error: {response_data}")
                        return None
                    
                    x_token = response_data.get("access_token")
                    if not x_token:
                        from sdk.security import sanitize_for_logging
                        logger.error(f"[Yandex] No access_token in response: {sanitize_for_logging(response_data)}")
                        return None
                    
                    logger.info(f"[Yandex] ✓ Got x_token (masked for security)")
                    return x_token

        except Exception as e:
            logger.error(f"[Yandex] Error getting x_token: {e}")
            return None

    async def _validate_x_token(self, session: aiohttp.ClientSession, x_token: str) -> Optional[Dict[str, Any]]:
        """Validate x_token and get account info.
        
        Args:
            session: aiohttp.ClientSession
            x_token: token to validate
        
        Returns:
            Dict with x_token, display_login, uid, etc. or None if invalid
        """
        try:
            async with session.get(
                "https://mobileproxy.passport.yandex.net/1/bundle/account/short_info/?avatar_size=islands-300",
                headers={"Authorization": f"OAuth {x_token}"},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"[Yandex] short_info failed: HTTP {resp.status}")
                    logger.error(f"[Yandex] short_info body (first 200): {body[:200]}")
                    return None

                # mobileproxy sometimes returns missing/incorrect content-type even with JSON.
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
                    body = await resp.text()
                    logger.error(f"[Yandex] short_info invalid JSON: {e}")
                    logger.error(f"[Yandex] short_info body (first 200): {body[:200]}")
                    return None

                if data.get("status") != "ok":
                    logger.error(f"[Yandex] Invalid x_token: {data}")
                    return None

                # Add x_token to response
                data["x_token"] = x_token
                display_login = data.get("display_login", data.get("login", "unknown"))
                logger.info(f"[Yandex] ✓ Successfully authenticated as {display_login}")
                return data

        except Exception as e:
            logger.error(f"[Yandex] Error validating x_token: {e}")
            return None
