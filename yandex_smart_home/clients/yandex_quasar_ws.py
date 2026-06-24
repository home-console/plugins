"""
Realtime клиент WebSocket Quasar для Яндекс Умного дома.

Перенесён в `clients` поскольку тесно связан с Quasar cookies и сетевым доступом.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set
import asyncio
import contextlib
import json
import random
import time
from urllib.parse import urlparse

import aiohttp
from aiohttp import ServerTimeoutError
from yarl import URL

from .api_client import YandexAPIClient
from ..transformers.device_transformer import DeviceTransformer
from .oauth_provider import get_cookies as oauth_get_cookies


class YandexQuasarWS:
    """WebSocket клиент для Quasar API (iot.quasar.yandex.ru).

    Использует cookies, НЕ OAuth token.
    """

    def __init__(self, plugin: Any, plugin_name: str):
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.api_client = YandexAPIClient(plugin, plugin_name)
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._runner: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._subscribers: Dict[str, Set[Callable[[Dict[str, Any]], Any]]] = {}
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._cookie_jar: Optional[aiohttp.CookieJar] = None
        self._current_cookies: Optional[Dict[str, str]] = None

    @property
    def runner(self) -> Optional[asyncio.Task]:
        return self._runner

    async def start(self) -> None:
        if self._runner and not self._runner.done():
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._runner:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    def subscribe(self, device_id: str, callback: Callable[[Dict[str, Any]], Any]) -> Callable[[], None]:
        self._subscribers.setdefault(device_id, set()).add(callback)

        def unsubscribe() -> None:
            self._subscribers.get(device_id, set()).discard(callback)

        return unsubscribe

    async def _run_loop(self) -> None:
        backoff = 1.0
        consecutive_errors = 0
        max_consecutive_errors = 10
        
        while not self._stop_event.is_set():
            try:
                cookies = await self._load_cookies()
                if not cookies:
                    await self._log(
                        "error",
                        "Quasar WS: cookies required but not found. Quasar API requires Yandex session cookies (Session_id, yandexuid). Use yandex.set_cookies service or configure oauth_yandex plugin to provide cookies."
                    )
                    await asyncio.sleep(30)
                    continue
                required = ["Session_id", "yandexuid"]
                missing = [k for k in required if k not in cookies]
                if missing:
                    await self._log(
                        "error",
                        f"Quasar WS: missing required cookies: {missing}. Have: {list(cookies.keys())}",
                        missing_cookies=missing,
                        available_cookies=list(cookies.keys())
                    )
                    await asyncio.sleep(30)
                    continue

                await self._log(
                    "info",
                    f"Quasar WS: using cookies for auth (NO OAuth token)",
                    cookie_count=len(cookies),
                    cookie_names=list(cookies.keys()),
                    has_session_id="Session_id" in cookies,
                    has_yandexuid="yandexuid" in cookies
                )
                
                devices, updates_url = await self._fetch_devices_and_url(cookies)
                await self._seed_and_publish(devices)
                backoff = 1.0
                consecutive_errors = 0
                await self._consume_ws(updates_url, cookies)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._log(
                    "warning",
                    f"[quasar_ws] Connection failed, reconnecting in {backoff}s: {type(e).__name__}: {e}",
                    error_type=type(e).__name__,
                    error_msg=str(e),
                )
                if self._stop_event.is_set():
                    break

                # Exponential backoff
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    break

                backoff = min(backoff * 2, 60)

                consecutive_errors += 1
                await self._log(
                    "error",
                    f"Quasar WS loop error: {type(e).__name__}: {e}",
                    error_type=type(e).__name__,
                    error_msg=str(e),
                    backoff=round(backoff, 2),
                    consecutive_errors=consecutive_errors,
                )

                if consecutive_errors >= max_consecutive_errors:
                    await self._log(
                        "error",
                        f"Quasar WS: too many consecutive errors ({consecutive_errors}), stopping reconnection attempts. Fix the issue and restart the plugin.",
                        consecutive_errors=consecutive_errors,
                    )
                    break

                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30.0)

    async def _fetch_devices_and_url(self, cookies: Dict[str, str]) -> tuple[List[Dict[str, Any]], str]:
        url = "https://iot.quasar.yandex.ru/m/v3/user/devices"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        assert "Authorization" not in headers, "NEVER use OAuth with Quasar API!"
        timeout = aiohttp.ClientTimeout(total=15)
        jar = self._cookie_jar_from(cookies)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar) as session:
            async with session.get(url, headers=headers) as resp:
                text = await resp.text()
                await self._log(
                    "debug",
                    f"Quasar devices response: HTTP {resp.status}",
                    status=resp.status,
                    response_preview=text[:200] if resp.status != 200 else "OK"
                )
                if resp.status != 200:
                    raise RuntimeError(f"Quasar devices HTTP {resp.status}: {text[:500]}. Hint: Quasar API requires valid Yandex session cookies, not OAuth token.")
                try:
                    data = json.loads(text)
                except Exception as parse_err:
                    raise RuntimeError(f"Quasar devices parse error: {parse_err} — {text[:200]}")
        updates_url = data.get("updates_url")
        devices = list(data.get("devices") or [])
        if not devices and data.get("status") == "ok":
            for house in data.get("households") or []:
                devices.extend(house.get("all") or [])
            await self._log("info", f"Quasar WS: loaded {len(devices)} devices from households", device_count=len(devices))
        if not updates_url:
            raise RuntimeError("updates_url missing in Quasar response")
        if not isinstance(updates_url, str):
            raise ValueError(f"Invalid updates_url type: {type(updates_url)}, expected str")
        if not updates_url.startswith(('ws://', 'wss://')):
            if updates_url.startswith('https://'):
                updates_url = updates_url.replace('https://', 'wss://', 1)
            elif updates_url.startswith('http://'):
                updates_url = updates_url.replace('http://', 'ws://', 1)
            else:
                updates_url = f"wss://{updates_url.lstrip('/')}"
        return devices, updates_url

    async def _consume_ws(self, updates_url: str, cookies: Dict[str, str]) -> None:
        if not updates_url or not isinstance(updates_url, str):
            raise ValueError(f"Invalid updates_url: {updates_url}")
        
        backoff_seconds = 1
        max_backoff = 60
        
        while not self._stop_event.is_set():
            try:
                if self._current_cookies != cookies:
                    if self._session and not self._session.closed:
                        await self._session.close()
                    self._cookie_jar = self._cookie_jar_from(cookies)
                    self._session = aiohttp.ClientSession(cookie_jar=self._cookie_jar)
                    self._current_cookies = cookies.copy()
                elif not self._session or self._session.closed:
                    self._cookie_jar = self._cookie_jar_from(cookies)
                    self._session = aiohttp.ClientSession(cookie_jar=self._cookie_jar)
                    self._current_cookies = cookies.copy()
                
                headers = {
                    "Origin": "https://iot.quasar.yandex.ru",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                }
                assert "Authorization" not in headers, "NEVER use OAuth with Quasar WebSocket!"
                
                try:
                    async with self._session.ws_connect(updates_url, headers=headers) as ws:
                        self._ws = ws
                        await self._log("info", "Quasar WS connected", url=updates_url[:80])
                        backoff_seconds = 1  # Reset backoff on successful connection
                        
                        async for msg in ws:
                            if self._stop_event.is_set():
                                break
                            
                            # Log message type
                            await self._log(
                                "debug",
                                f"[quasar_ws] Received WS message",
                                msg_type=str(msg.type),
                            )
                            
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(msg.data)
                            elif msg.type == aiohttp.WSMsgType.CLOSED:
                                await self._log(
                                    "warning",
                                    f"[quasar_ws] WebSocket closed",
                                    code=ws.close_code,
                                    reason=ws.close_reason,
                                )
                                raise RuntimeError(f"WebSocket closed: {ws.close_reason}")
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                exc = ws.exception()
                                await self._log(
                                    "error",
                                    f"[quasar_ws] WebSocket error: {exc}",
                                )
                                raise exc or RuntimeError("WebSocket error")
                        
                        # Normal exit (stop_event set)
                        await self._log("info", "Quasar WS closed (stop requested)")
                        break
                        
                except (TypeError, AttributeError) as e:
                    if "raw_host" in str(e) or "str" in str(e):
                        await self._log("debug", f"Retrying WS connect with URL object: {e}")
                        ws_url = URL(updates_url)
                        async with self._session.ws_connect(ws_url, headers=headers) as ws:
                            self._ws = ws
                            await self._log("info", "Quasar WS connected (via URL object)", url=updates_url[:80])
                            backoff_seconds = 1
                            
                            async for msg in ws:
                                if self._stop_event.is_set():
                                    break
                                
                                await self._log(
                                    "debug",
                                    f"[quasar_ws] Received WS message",
                                    msg_type=str(msg.type),
                                )
                                
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    await self._handle_message(msg.data)
                                elif msg.type == aiohttp.WSMsgType.CLOSED:
                                    await self._log(
                                        "warning",
                                        f"[quasar_ws] WebSocket closed",
                                        code=ws.close_code,
                                        reason=ws.close_reason,
                                    )
                                    raise RuntimeError(f"WebSocket closed: {ws.close_reason}")
                                elif msg.type == aiohttp.WSMsgType.ERROR:
                                    exc = ws.exception()
                                    await self._log(
                                        "error",
                                        f"[quasar_ws] WebSocket error: {exc}",
                                    )
                                    raise exc or RuntimeError("WebSocket error")
                            
                            await self._log("info", "Quasar WS closed (stop requested)")
                            break
                    else:
                        raise
                        
            except Exception as e:
                await self._log(
                    "warning",
                    f"[quasar_ws] Connection failed, reconnecting in {backoff_seconds}s: {type(e).__name__}: {e}",
                )
                
                if self._stop_event.is_set():
                    break
                
                # Exponential backoff
                try:
                    await asyncio.sleep(backoff_seconds)
                except asyncio.CancelledError:
                    break
                
                backoff_seconds = min(backoff_seconds * 2, max_backoff)

    async def _handle_message(self, raw: str) -> None:
        try:
            envelope = json.loads(raw)
            
            # DEBUG 2: Log raw envelope
            try:
                op = envelope.get("operation", "unknown")
                msg_id = envelope.get("message_id", "N/A")
                await self._log(
                    "debug",
                    f"[WS_RAW_ENVELOPE] Message received",
                    operation=op,
                    message_id=msg_id,
                    has_payload=bool(envelope.get("message")),
                )
            except Exception:
                pass
            
            if envelope.get("operation") != "update_states":
                return
            payload_raw = envelope.get("message")
            payload = json.loads(payload_raw) if payload_raw else {}
            updated = payload.get("updated_devices") or []
            for device in updated:
                await self._process_device_update(device)
        except Exception as e:
            await self._log("error", f"Failed to process WS message: {type(e).__name__}: {e}")

    async def _process_device_update(self, device: Dict[str, Any]) -> None:
        device_id = device.get("id") or device.get("device_id")
        if not device_id:
            return

        # DEBUG 2B: Log raw device payload
        try:
            ws_timestamp = time.time()
            await self.plugin.storage_set(
                "yandex_debug_ws_raw",
                f"{device_id}_{int(ws_timestamp * 1000)}",
                {
                    "timestamp": ws_timestamp,
                    "external_id": device_id,
                    "raw_device": device,
                },
            )
        except Exception:
            pass

        # Используем новый метод для извлечения capabilities и state
        caps_list, cap_state = DeviceTransformer._extract_capabilities(device.get("capabilities", []))
        
        # Извлекаем properties и их состояния
        prop_list, prop_state = DeviceTransformer._extract_properties(device.get("properties", []))
        
        # Объединяем состояния: properties переопределяют capabilities
        state = {**cap_state, **prop_state}
        
        # Также учитываем states если они есть (для back-compat)
        states_list: List[Dict[str, Any]] = []
        if isinstance(device.get("states"), list):
            states_list.extend(device.get("states") or [])
        if isinstance(device.get("state"), list):
            states_list.extend(device.get("state") or [])
        
        # Если есть states список - применяем извлечение с back-compat методом
        if states_list:
            old_state = DeviceTransformer._extract_state(states_list, None)
            state.update(old_state)
        
        # DEBUG 2C: Log extracted state
        try:
            await self.plugin.storage_set(
                "yandex_debug_ws_parsed",
                f"{device_id}_{int(ws_timestamp * 1000)}",
                {
                    "timestamp": ws_timestamp,
                    "external_id": device_id,
                    "extracted_state": state,
                    "raw_capabilities": device.get("capabilities", []),
                    "raw_states": states_list,
                },
            )
            await self._log(
                "debug",
                f"[WS_PARSED] Extracted state",
                external_id=device_id,
                state_keys=list((state or {}).keys()),
            )
        except Exception:
            pass
        self._devices[device_id] = {"state": state or {}, "raw": device}
        await self._publish_state(device_id, state or {})
        await self._log(
            "debug",
            f"Processed device update from WS",
            device_id=device_id,
            state=state,
            has_on="on" in (state or {}),
        )

    async def _seed_and_publish(self, devices: List[Dict[str, Any]]) -> None:
        for device in devices:
            device_id = device.get("id")
            if not device_id:
                continue
            
            # Используем новый метод для извлечения capabilities и state
            caps_list, cap_state = DeviceTransformer._extract_capabilities(device.get("capabilities", []))
            prop_list, prop_state = DeviceTransformer._extract_properties(device.get("properties", []))
            
            # Объединяем состояния
            state = {**cap_state, **prop_state}
            
            # Также учитываем states если они есть
            raw_states = device.get("states") or []
            if raw_states:
                old_state = DeviceTransformer._extract_state(raw_states, None)
                state.update(old_state)
            
            if state:
                self._devices[device_id] = {"state": state, "raw": device}
                await self._publish_state(device_id, state)

    async def _publish_state(self, device_id: str, state: Dict[str, Any]) -> None:
        payload = {"external_id": device_id, "state": state, "source": "ws"}
        await self._log(
            "debug",
            f"Publishing state update from WS",
            external_id=device_id,
            state=state,
        )
        try:
            await self.plugin.publish_event("external.device_state_reported", payload)
            await self._log(
                "debug",
                f"State update published successfully",
                external_id=device_id,
            )
        except Exception as e:
            await self._log(
                "error",
                f"Failed to publish state update: {e}",
                external_id=device_id,
                state=state,
            )
        for cb in list(self._subscribers.get(device_id, [])):
            try:
                result = cb(payload)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                continue

    def _cookie_jar_from(self, cookies: Optional[Dict[str, str]]) -> aiohttp.CookieJar:
        jar = aiohttp.CookieJar(unsafe=True)
        if cookies:
            base_url = URL("https://iot.quasar.yandex.ru")
            from http.cookies import SimpleCookie
            cookie_dict = SimpleCookie()
            for name, value in cookies.items():
                cookie_dict[name] = str(value)
                cookie_dict[name]["domain"] = ".yandex.ru"
                cookie_dict[name]["path"] = "/"
            jar.update_cookies(cookie_dict, response_url=base_url)
            yandex_url = URL("https://yandex.ru")
            jar.update_cookies(cookie_dict, response_url=yandex_url)
        return jar

    async def _load_cookies(self) -> Optional[Dict[str, str]]:
        cookies = await oauth_get_cookies(self.plugin)
        if cookies:
            await self._log("debug", "Loaded cookies via oauth_provider", cookie_count=len(cookies))
        return cookies

    async def _log(self, level: str, message: str, **context: Any) -> None:
        with contextlib.suppress(Exception):
            await self.plugin.call_service(
                "logger.log",
                level=level,
                message=message,
                plugin=self.plugin_name,
                context=context or None,
            )
