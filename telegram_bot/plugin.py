"""
Telegram Bot плагин — канал уведомлений для alerting плагина.

Подписывается на alert.triggered и отправляет сообщение в Telegram чат.
Никакой связности с alerting плагином нет — только через событие.

Конфиг через env:
  TELEGRAM_BOT_TOKEN   — токен бота (обязательно)
  TELEGRAM_CHAT_ID     — ID чата или @channel (обязательно)
  TELEGRAM_PARSE_MODE  — HTML | Markdown | MarkdownV2 (по умолчанию HTML)

Пример сообщения:
  🔴 <b>WireGuard туннель упал</b>
  Туннель vds→apt1 в сети default мертво
  Severity: critical · 14:32:01
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from sdk.plugin_ext import BasePlugin, PluginMetadata

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    "info":     "ℹ️",
    "warning":  "⚠️",
    "critical": "🔴",
}


class TelegramBotPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="telegram_bot",
        version="0.1.0",
        description="Telegram-канал уведомлений (подписывается на alert.triggered)",
    )

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._token: str = ""
        self._chat_id: str = ""
        self._parse_mode: str = "HTML"

    # ------------------------------------------------------------------
    # Lifecycle

    async def on_load(self) -> None:
        self._token      = self.get_env_config("TELEGRAM_BOT_TOKEN",  "")
        self._chat_id    = self.get_env_config("TELEGRAM_CHAT_ID",    "")
        self._parse_mode = self.get_env_config("TELEGRAM_PARSE_MODE", "HTML")

        ui_cfg = await self._load_ui_config()
        if not self._token:
            self._token = ui_cfg.get("TELEGRAM_BOT_TOKEN", "")
        if not self._chat_id:
            self._chat_id = ui_cfg.get("TELEGRAM_CHAT_ID", "")
        if self._parse_mode == "HTML":
            self._parse_mode = ui_cfg.get("TELEGRAM_PARSE_MODE", "HTML")

        if not self._token or not self._chat_id:
            logger.warning(
                "telegram_bot: TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID не заданы — "
                "плагин загружен, но уведомления не будут отправляться"
            )

        await self._register_services()

    async def on_start(self) -> None:
        # Подписываемся на все alert.triggered — этим и становимся каналом
        await self.subscribe_event("alert.triggered", self._on_alert)
        logger.info("telegram_bot: подписан на alert.triggered")

    # ------------------------------------------------------------------
    # Alert handler

    async def _on_alert(self, payload: dict[str, Any]) -> None:
        if not self._token or not self._chat_id:
            return

        severity = payload.get("severity", "info")
        emoji    = SEVERITY_EMOJI.get(severity, "ℹ️")
        name     = payload.get("rule_name", payload.get("rule_id", "?"))
        message  = payload.get("message", "")
        ts       = payload.get("fired_at", int(time.time()))
        ts_str   = time.strftime("%H:%M:%S", time.gmtime(ts))

        text = (
            f"{emoji} <b>{_escape_html(name)}</b>\n"
            f"{_escape_html(message)}\n"
            f"<i>Severity: {severity} · {ts_str} UTC</i>"
        )

        await self._send(text)

    # ------------------------------------------------------------------
    # Services

    async def _register_services(self) -> None:
        async def send(text: str, chat_id: str | None = None, **_: Any) -> dict[str, Any]:
            """Отправить произвольное сообщение в Telegram."""
            ok, err = await self._send(text, chat_id=chat_id)
            return {"ok": ok, "error": err}

        async def status(**_: Any) -> dict[str, Any]:
            """Статус плагина: настроен ли бот."""
            configured = bool(self._token and self._chat_id)
            return {
                "ok": True,
                "configured": configured,
                "chat_id": self._chat_id if configured else None,
                "parse_mode": self._parse_mode,
            }

        await self.register_service("telegram_bot.send",   send)
        await self.register_service("telegram_bot.status", status)

    # ------------------------------------------------------------------
    # HTTP send

    async def _load_ui_config(self) -> dict[str, str]:
        try:
            raw = await self.storage_get(self.metadata.name, "ui_config")
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    async def _send(self, text: str, chat_id: str | None = None) -> tuple[bool, str]:
        if not self._token:
            return False, "TELEGRAM_BOT_TOKEN не задан"
        target = chat_id or self._chat_id
        if not target:
            return False, "TELEGRAM_CHAT_ID не задан"

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json={
                    "chat_id":    target,
                    "text":       text,
                    "parse_mode": self._parse_mode,
                })
            if r.is_success:
                return True, ""
            body = r.json()
            err  = body.get("description", f"HTTP {r.status_code}")
            logger.warning("telegram_bot: ошибка отправки: %s", err)
            return False, err
        except httpx.HTTPError as e:
            logger.error("telegram_bot: сетевая ошибка: %s", e)
            return False, str(e)


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
