"""
Webhook плагин — канал уведомлений для alerting плагина.

Подписывается на alert.triggered и отправляет HTTP POST на один или несколько
настраиваемых URL. Эндпоинты хранятся в Core storage (CRUD через сервисы).

Конфиг через env (быстрый старт с одним эндпоинтом):
  WEBHOOK_URL      — URL для POST (если задан, автоматически добавляется как "default")
  WEBHOOK_SECRET   — секрет в заголовок X-Webhook-Secret
  WEBHOOK_TIMEOUT  — таймаут в секундах (по умолчанию 10)

Сервисы:
  webhook.add_endpoint(url, name, secret, timeout, severity_filter, enabled)
  webhook.remove_endpoint(endpoint_id)
  webhook.list_endpoints()
  webhook.send(url, body, secret)   — отправить произвольно
  webhook.status()

Тело POST-запроса (JSON):
  {
    "event":        "alert.triggered",
    "rule_id":      "...",
    "rule_name":    "...",
    "severity":     "info|warning|critical",
    "message":      "...",
    "payload":      {...},
    "fired_at":     1234567890,
    "fired_at_iso": "2026-06-24T12:00:00Z"
  }

Подписи (если задан secret):
  X-Webhook-Secret:    <secret>
  X-Webhook-Signature: sha256=<hmac-sha256(secret, json_body)>
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import time
import uuid
from typing import Any

import httpx

from sdk.plugin_ext import BasePlugin, PluginMetadata

logger = logging.getLogger(__name__)

_NS          = "webhook"
_KEY         = "endpoints"
_SEVERITIES  = {"info", "warning", "critical"}


class WebhookPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="webhook",
        version="0.2.0",
        description="Webhook-канал уведомлений (POST на URL при alert.triggered)",
    )

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._default_timeout = 10.0

    # ------------------------------------------------------------------
    # Lifecycle

    async def on_load(self) -> None:
        self._default_timeout = float(self.get_env_config("WEBHOOK_TIMEOUT", "10"))
        await self._register_services()
        await self._register_http()

    async def on_start(self) -> None:
        # Env-эндпоинт → добавляем как "default" если задан и его ещё нет
        env_url = self.get_env_config("WEBHOOK_URL", "")
        if env_url:
            eps = await self._load_endpoints()
            if "default" not in eps:
                eps["default"] = _make_ep(
                    ep_id="default",
                    name="default (env)",
                    url=env_url,
                    secret=self.get_env_config("WEBHOOK_SECRET", ""),
                    timeout=self._default_timeout,
                )
                await self._save_endpoints(eps)
                logger.info("webhook: добавлен эндпоинт 'default' из WEBHOOK_URL")

        await self.subscribe_event("alert.triggered", self._on_alert)
        eps    = await self._load_endpoints()
        active = sum(1 for e in eps.values() if e.get("enabled"))
        logger.info("webhook: подписан на alert.triggered, активных эндпоинтов: %d", active)

    # ------------------------------------------------------------------
    # Storage

    async def _load_endpoints(self) -> dict[str, Any]:
        data = await self.storage_get(_NS, _KEY)
        return data if isinstance(data, dict) else {}

    async def _save_endpoints(self, eps: dict[str, Any]) -> None:
        await self.storage_set(_NS, _KEY, eps)

    # ------------------------------------------------------------------
    # Alert handler

    async def _on_alert(self, payload: dict[str, Any]) -> None:
        eps = await self._load_endpoints()
        if not eps:
            return

        severity = payload.get("severity", "info")
        body = _build_body(payload, severity)

        for ep_id, ep in eps.items():
            if not ep.get("enabled", True):
                continue
            sf = ep.get("severity_filter") or []
            if sf and severity not in sf:
                continue

            ok, err = await self._send_to(ep, body)
            await self._update_stats(ep_id, ok, err)

            try:
                await self.publish_event(
                    "webhook.sent" if ok else "webhook.failed",
                    {"endpoint_id": ep_id, "url": ep.get("url"), "ok": ok, "error": err,
                     "rule_id": payload.get("rule_id")},
                )
            except Exception:
                pass

    async def _update_stats(self, ep_id: str, ok: bool, err: str) -> None:
        try:
            eps = await self._load_endpoints()
            if ep_id not in eps:
                return
            eps[ep_id]["send_count"]   = eps[ep_id].get("send_count", 0) + 1
            eps[ep_id]["last_sent_at"] = int(time.time())
            eps[ep_id]["last_error"]   = None if ok else err
            await self._save_endpoints(eps)
        except Exception:
            pass

    async def _send_to(
        self, ep: dict[str, Any], body: dict[str, Any]
    ) -> tuple[bool, str]:
        url     = ep.get("url", "")
        secret  = ep.get("secret", "")
        timeout = float(ep.get("timeout") or self._default_timeout)

        if not url:
            return False, "url не задан"

        body_bytes = json.dumps(body, ensure_ascii=False).encode()
        headers    = {"Content-Type": "application/json"}
        if secret:
            headers["X-Webhook-Secret"] = secret
            sig = _hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={sig}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, content=body_bytes, headers=headers)
            if r.is_success:
                logger.debug("webhook: ✓ %s (%d)", url, r.status_code)
                return True, ""
            err = f"HTTP {r.status_code}: {r.text[:200]}"
            logger.warning("webhook: ✗ %s — %s", url, err)
            return False, err
        except httpx.TimeoutException:
            err = f"timeout ({timeout}s)"
            logger.warning("webhook: ✗ %s — %s", url, err)
            return False, err
        except httpx.HTTPError as e:
            logger.error("webhook: ✗ %s — %s", url, e)
            return False, str(e)

    # ------------------------------------------------------------------
    # Services

    async def _register_services(self) -> None:

        async def add_endpoint(
            url: str,
            name: str = "",
            secret: str = "",
            timeout: float = 10.0,
            severity_filter: list[str] | None = None,
            enabled: bool = True,
            **_: Any,
        ) -> dict[str, Any]:
            if not url:
                return {"ok": False, "error": "url обязателен"}
            sf    = [s for s in (severity_filter or []) if s in _SEVERITIES]
            ep_id = uuid.uuid4().hex[:10]
            ep    = _make_ep(ep_id, name or url, url, secret, float(timeout), sf, enabled)
            eps   = await self._load_endpoints()
            eps[ep_id] = ep
            await self._save_endpoints(eps)
            logger.info("webhook: добавлен эндпоинт '%s' → %s", name or ep_id, url)
            return {"ok": True, "endpoint": ep}

        async def remove_endpoint(endpoint_id: str, **_: Any) -> dict[str, Any]:
            eps = await self._load_endpoints()
            if endpoint_id not in eps:
                return {"ok": False, "error": f"Эндпоинт '{endpoint_id}' не найден"}
            del eps[endpoint_id]
            await self._save_endpoints(eps)
            return {"ok": True, "endpoint_id": endpoint_id}

        async def list_endpoints(**_: Any) -> dict[str, Any]:
            eps = await self._load_endpoints()
            return {"ok": True, "endpoints": list(eps.values()), "total": len(eps)}

        async def send(
            url: str,
            body: dict[str, Any] | None = None,
            secret: str = "",
            **_: Any,
        ) -> dict[str, Any]:
            ep = {"url": url, "secret": secret, "timeout": self._default_timeout}
            ok, err = await self._send_to(ep, body or {})
            return {"ok": ok, "error": err}

        async def status(**_: Any) -> dict[str, Any]:
            eps    = await self._load_endpoints()
            active = sum(1 for e in eps.values() if e.get("enabled"))
            return {
                "ok":               True,
                "total_endpoints":  len(eps),
                "active_endpoints": active,
                "endpoints": [
                    {
                        "id":           e["id"],
                        "name":         e.get("name"),
                        "url":          e.get("url"),
                        "enabled":      e.get("enabled"),
                        "send_count":   e.get("send_count", 0),
                        "last_sent_at": e.get("last_sent_at"),
                        "last_error":   e.get("last_error"),
                    }
                    for e in eps.values()
                ],
            }

        await self.register_service("webhook.add_endpoint",    add_endpoint)
        await self.register_service("webhook.remove_endpoint", remove_endpoint)
        await self.register_service("webhook.list_endpoints",  list_endpoints)
        await self.register_service("webhook.send",            send)
        await self.register_service("webhook.status",          status)

    # ------------------------------------------------------------------
    # HTTP endpoints

    async def _register_http(self) -> None:
        try:
            from sdk.http import EndpointAuthConfig, HttpEndpoint
        except ImportError:
            return

        _r = EndpointAuthConfig(required_scopes=["admin.read"])
        _w = EndpointAuthConfig(required_scopes=["admin.write"])
        b  = "/api/v1/plugins/webhook"

        for ep in [
            HttpEndpoint(method="GET",    path=f"{b}/endpoints",                   service="webhook.list_endpoints",  description="Список webhook эндпоинтов",     auth_config=_r),
            HttpEndpoint(method="POST",   path=f"{b}/endpoints",                   service="webhook.add_endpoint",    description="Добавить эндпоинт",             auth_config=_w),
            HttpEndpoint(method="DELETE", path=f"{b}/endpoints/{{endpoint_id}}",   service="webhook.remove_endpoint", description="Удалить эндпоинт",              auth_config=_w),
            HttpEndpoint(method="GET",    path=f"{b}/status",                      service="webhook.status",          description="Статус всех эндпоинтов",        auth_config=_r),
            HttpEndpoint(method="POST",   path=f"{b}/send",                        service="webhook.send",            description="Отправить произвольный запрос", auth_config=_w),
        ]:
            self.register_http_endpoint(ep)


# ---------------------------------------------------------------------------

def _make_ep(
    ep_id: str,
    name: str,
    url: str,
    secret: str = "",
    timeout: float = 10.0,
    severity_filter: list[str] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "id":              ep_id,
        "name":            name,
        "url":             url,
        "secret":          secret,
        "timeout":         timeout,
        "severity_filter": severity_filter or [],
        "enabled":         enabled,
        "created_at":      int(time.time()),
        "send_count":      0,
        "last_sent_at":    None,
        "last_error":      None,
    }


def _build_body(payload: dict[str, Any], severity: str) -> dict[str, Any]:
    ts = payload.get("fired_at", time.time())
    return {
        "event":        "alert.triggered",
        "rule_id":      payload.get("rule_id"),
        "rule_name":    payload.get("rule_name"),
        "severity":     severity,
        "message":      payload.get("message", ""),
        "payload":      payload.get("payload", {}),
        "fired_at":     int(ts),
        "fired_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
    }
