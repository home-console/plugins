"""
Прокси-плагин для управления remote plugins из Core Runtime.

Позволяет загружать и управлять удалёнными сервисами через HTTP контракт,
не изменяя Core Runtime.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

try:
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None

from sdk.plugin_ext import BasePlugin, PluginMetadata


class RemotePluginProxy(BasePlugin):
    """Локальный прокси для удалённого плагина."""

    def __init__(self, runtime: Any, remote_url: Optional[str] = None):
        super().__init__(runtime)

        if remote_url is None:
            remote_url = self.get_env_config("REMOTE_URL", prefix="REMOTE_PLUGIN")
            if not remote_url:
                remote_url = self.get_env_config("REMOTE_PLUGIN_URL")

        if not remote_url:
            raise ValueError(
                "remote_url обязателен для RemotePluginProxy. "
                "Укажите при создании или установите переменную окружения "
                "REMOTE_PLUGIN_PROXY_REMOTE_URL, REMOTE_PLUGIN_URL или REMOTE_URL"
            )

        self.remote_url = remote_url
        self._metadata: Optional[dict] = None
        self._registered_services: list[str] = []
        self._http_timeout = 3

    async def _http_call(
        self, endpoint: str, method: str = "GET", json_data: Optional[dict] = None
    ) -> dict:
        url = f"{self.remote_url}{endpoint}"

        if aiohttp is None:
            def _sync_call():
                import urllib.request

                req_url = url
                if method == "GET":
                    with urllib.request.urlopen(req_url, timeout=self._http_timeout) as resp:
                        return json.loads(resp.read().decode())
                if method == "POST":
                    req = urllib.request.Request(
                        req_url,
                        data=json.dumps(json_data or {}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:
                        return json.loads(resp.read().decode())
                raise ValueError(f"Unsupported method: {method}")

            try:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, _sync_call)
            except Exception as exc:
                try:
                    await self.call_service(
                        "logger.log",
                        level="error",
                        message=f"RemotePluginProxy: {endpoint} failed: {str(exc)}",
                    )
                except Exception:
                    pass
                raise RuntimeError(f"RemotePluginProxy sync http error: {exc}")

        try:
            timeout = aiohttp.ClientTimeout(total=self._http_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if method == "GET":
                    async with await session.get(url) as resp:
                        return json.loads(await resp.text())
                if method == "POST":
                    async with await session.post(url, json=json_data or {}) as resp:
                        return json.loads(await resp.text())
                raise ValueError(f"Unsupported method: {method}")
        except Exception as exc:
            try:
                await self.call_service(
                    "logger.log",
                    level="error",
                    message=f"RemotePluginProxy: {endpoint} failed: {str(exc)}",
                )
            except Exception:
                pass
            raise RuntimeError(f"RemotePluginProxy http error: {exc}")

    async def _fetch_metadata(self) -> dict:
        return await self._http_call("/plugin/metadata")

    @property
    def metadata(self) -> PluginMetadata:
        try:
            if not isinstance(self._metadata, dict):
                return PluginMetadata(
                    name="remote_plugin",
                    version="0.0.0",
                    description="Remote plugin proxy",
                    author="Home Console",
                    dynamic_service_registration=True,
                )
            return PluginMetadata(
                name=self._metadata.get("name", "remote_plugin"),
                version=self._metadata.get("version", "0.0.0"),
                description=self._metadata.get("description", ""),
                author=self._metadata.get("author", ""),
                dynamic_service_registration=True,
            )
        except Exception:
            return PluginMetadata(
                name="remote_plugin",
                version="0.0.0",
                description="Remote plugin proxy",
                author="Home Console",
                dynamic_service_registration=True,
            )

    async def on_load(self) -> None:
        await super().on_load()
        self._ready = False

        try:
            self._metadata = await self._fetch_metadata()
        except Exception as exc:
            try:
                await self.call_service(
                    "logger.log",
                    level="warning",
                    message=f"RemotePluginProxy: failed to fetch metadata: {exc}",
                )
            except Exception:
                pass
            raise RuntimeError(
                f"RemotePluginProxy load failed: metadata fetch error: {exc}"
            ) from exc

        try:
            await self._http_call("/plugin/load", method="POST")
        except Exception as exc:
            try:
                await self.call_service(
                    "logger.log",
                    level="warning",
                    message=f"RemotePluginProxy: /plugin/load failed: {exc}",
                )
            except Exception:
                pass
            raise RuntimeError(
                f"RemotePluginProxy load failed: remote /plugin/load error: {exc}"
            ) from exc

        services = (
            self._metadata.get("services", []) if isinstance(self._metadata, dict) else []
        )
        for svc in services:
            svc_name = svc.get("name")
            endpoint = svc.get("endpoint")
            method = str(svc.get("method", "POST")).upper()
            if not svc_name or not endpoint:
                continue

            async def _make_forwarder(_endpoint=endpoint, _method=method):
                async def _forward(*args, **kwargs):
                    payload = {"args": args, "kwargs": kwargs}
                    try:
                        if _method == "GET":
                            return await self._http_call(_endpoint, method="GET")
                        return await self._http_call(
                            _endpoint, method="POST", json_data=payload
                        )
                    except Exception as exc:
                        try:
                            await self.call_service(
                                "logger.log",
                                level="error",
                                message=f"RemotePluginProxy forwarder error for {_endpoint}: {exc}",
                            )
                        except Exception:
                            pass
                        raise RuntimeError(f"remote forwarder error: {exc}")

                return _forward

            forwarder = await _make_forwarder()
            try:
                await self.register_service(svc_name, forwarder)
                self._registered_services.append(svc_name)
            except Exception:
                try:
                    await self.call_service(
                        "logger.log",
                        level="warning",
                        message=f"RemotePluginProxy: не удалось зарегистрировать сервис {svc_name}",
                    )
                except Exception:
                    pass

        self._ready = True

    async def on_start(self) -> None:
        await super().on_start()
        try:
            await self._http_call("/plugin/start", method="POST")
        except Exception:
            pass

    async def on_stop(self) -> None:
        await super().on_stop()
        try:
            await self._http_call("/plugin/stop", method="POST")
        except Exception:
            pass

    async def on_unload(self) -> None:
        await super().on_unload()
        try:
            await self._http_call("/plugin/unload", method="POST")
        except Exception:
            pass

        for svc_name in list(self._registered_services):
            try:
                await self.unregister_service(svc_name)
            except Exception:
                pass
        self._registered_services.clear()


__all__ = ["RemotePluginProxy"]
