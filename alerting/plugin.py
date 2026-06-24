"""
Alerting плагин для HomeConsole Core.

Управляет правилами алертинга. Каждое правило:
  - слушает определённый тип события (event_pattern)
  - при срабатывании публикует alert.triggered

Каналы уведомлений (Telegram, webhook, email и т.д.) — отдельные плагины,
которые подписываются на alert.triggered и делают что нужно.
Никакой связности между alerting и каналами нет.

Сервисы:
  alerting.create_rule(name, event_pattern, severity, message_template, ...)
  alerting.update_rule(rule_id, **fields)
  alerting.delete_rule(rule_id)
  alerting.list_rules()
  alerting.get_rule(rule_id)
  alerting.enable_rule(rule_id) / disable_rule(rule_id)
  alerting.recent_alerts(limit)

Конфиг через env:
  ALERTING_MAX_HISTORY  — максимум хранимых срабатываний (по умолчанию 200)
"""
from __future__ import annotations

import logging
import time
import uuid
from string import Formatter
from typing import Any

from sdk.plugin_ext import BasePlugin, PluginMetadata

logger = logging.getLogger(__name__)

NS         = "alerting"
KEY_RULES  = "rules"
KEY_HIST   = "history"

SEVERITIES = ("info", "warning", "critical")


class AlertingPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="alerting",
        version="0.1.0",
        description="Правила алертинга — публикует alert.triggered при срабатывании",
    )

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._max_history = 200
        # event_type → set of rule_ids которые его слушают
        self._subscribed: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle

    async def on_load(self) -> None:
        self._max_history = self.get_env_config_int("ALERTING_MAX_HISTORY", 200)
        await self._register_services()
        await self._register_http()

    async def on_start(self) -> None:
        # Подписываемся на все события из существующих правил
        rules = await self._load_rules()
        for rule in rules.values():
            if rule.get("enabled", True):
                await self._ensure_subscribed(rule["event_pattern"])
        logger.info("alerting: загружено %d правил", len(rules))

    # ------------------------------------------------------------------
    # Storage helpers

    async def _load_rules(self) -> dict[str, Any]:
        data = await self.storage_get(NS, KEY_RULES)
        return data if isinstance(data, dict) else {}

    async def _save_rules(self, rules: dict[str, Any]) -> None:
        await self.storage_set(NS, KEY_RULES, rules)

    async def _load_history(self) -> list[dict[str, Any]]:
        data = await self.storage_get(NS, KEY_HIST)
        return data if isinstance(data, list) else []

    async def _append_history(self, entry: dict[str, Any]) -> None:
        hist = await self._load_history()
        hist.append(entry)
        if len(hist) > self._max_history:
            hist = hist[-self._max_history:]
        await self.storage_set(NS, KEY_HIST, hist)

    # ------------------------------------------------------------------
    # Subscription management

    async def _ensure_subscribed(self, event_pattern: str) -> None:
        if event_pattern in self._subscribed:
            return
        await self.subscribe_event(event_pattern, self._make_handler(event_pattern))
        self._subscribed.add(event_pattern)
        logger.debug("alerting: подписан на '%s'", event_pattern)

    def _make_handler(self, event_pattern: str):
        async def handler(payload: dict[str, Any]) -> None:
            await self._on_event(event_pattern, payload)
        return handler

    async def _on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        rules = await self._load_rules()
        now   = int(time.time())

        for rule_id, rule in rules.items():
            if not rule.get("enabled", True):
                continue
            if rule.get("event_pattern") != event_type:
                continue

            # Throttle: не спамить чаще чем раз в N секунд
            throttle = int(rule.get("throttle_sec", 60))
            last_fired = rule.get("last_fired_at", 0)
            if throttle > 0 and (now - last_fired) < throttle:
                continue

            # Формируем сообщение
            template = rule.get("message_template", "{event_type} сработал")
            message  = _safe_format(template, event_type=event_type, **payload)

            alert = {
                "rule_id":    rule_id,
                "rule_name":  rule.get("name", rule_id),
                "event_type": event_type,
                "severity":   rule.get("severity", "info"),
                "message":    message,
                "payload":    payload,
                "fired_at":   now,
            }

            # Обновляем last_fired_at
            rule["last_fired_at"] = now
            rules[rule_id] = rule
            await self._save_rules(rules)

            # Публикуем событие (любой плагин-канал подхватит)
            await self.publish_event("alert.triggered", alert)
            await self._append_history(alert)

            logger.info(
                "alerting: правило '%s' сработало (%s) — %s",
                rule.get("name", rule_id), rule.get("severity"), message,
            )

    # ------------------------------------------------------------------
    # Service registration

    async def _register_services(self) -> None:

        async def create_rule(
            name: str,
            event_pattern: str,
            message_template: str = "{event_type} сработал",
            severity: str = "warning",
            throttle_sec: int = 60,
            enabled: bool = True,
            **_: Any,
        ) -> dict[str, Any]:
            if severity not in SEVERITIES:
                return {"ok": False, "error": f"severity должен быть одним из {SEVERITIES}"}

            rules   = await self._load_rules()
            rule_id = str(uuid.uuid4())[:8]
            rule = {
                "id":               rule_id,
                "name":             name,
                "event_pattern":    event_pattern,
                "message_template": message_template,
                "severity":         severity,
                "throttle_sec":     throttle_sec,
                "enabled":          enabled,
                "created_at":       int(time.time()),
                "last_fired_at":    0,
            }
            rules[rule_id] = rule
            await self._save_rules(rules)

            if enabled:
                await self._ensure_subscribed(event_pattern)

            return {"ok": True, "rule": rule}

        async def update_rule(rule_id: str, **fields: Any) -> dict[str, Any]:
            rules = await self._load_rules()
            if rule_id not in rules:
                return {"ok": False, "error": f"Правило '{rule_id}' не найдено"}
            rule = rules[rule_id]
            for k, v in fields.items():
                if k not in ("id", "created_at", "last_fired_at"):
                    rule[k] = v
            rules[rule_id] = rule
            await self._save_rules(rules)
            if rule.get("enabled"):
                await self._ensure_subscribed(rule["event_pattern"])
            return {"ok": True, "rule": rule}

        async def delete_rule(rule_id: str, **_: Any) -> dict[str, Any]:
            rules = await self._load_rules()
            if rule_id not in rules:
                return {"ok": False, "error": f"Правило '{rule_id}' не найдено"}
            del rules[rule_id]
            await self._save_rules(rules)
            return {"ok": True, "rule_id": rule_id}

        async def list_rules(**_: Any) -> dict[str, Any]:
            rules = await self._load_rules()
            return {"ok": True, "rules": list(rules.values()), "total": len(rules)}

        async def get_rule(rule_id: str, **_: Any) -> dict[str, Any]:
            rules = await self._load_rules()
            rule  = rules.get(rule_id)
            if rule is None:
                return {"ok": False, "error": f"Правило '{rule_id}' не найдено"}
            return {"ok": True, "rule": rule}

        async def enable_rule(rule_id: str, **_: Any) -> dict[str, Any]:
            return await update_rule(rule_id, enabled=True)

        async def disable_rule(rule_id: str, **_: Any) -> dict[str, Any]:
            return await update_rule(rule_id, enabled=False)

        async def recent_alerts(limit: int = 50, **_: Any) -> dict[str, Any]:
            hist = await self._load_history()
            return {"ok": True, "alerts": hist[-limit:][::-1], "total": len(hist)}

        await self.register_service("alerting.create_rule",  create_rule)
        await self.register_service("alerting.update_rule",  update_rule)
        await self.register_service("alerting.delete_rule",  delete_rule)
        await self.register_service("alerting.list_rules",   list_rules)
        await self.register_service("alerting.get_rule",     get_rule)
        await self.register_service("alerting.enable_rule",  enable_rule)
        await self.register_service("alerting.disable_rule", disable_rule)
        await self.register_service("alerting.recent_alerts", recent_alerts)

    # ------------------------------------------------------------------
    # HTTP endpoints

    async def _register_http(self) -> None:
        try:
            from sdk.http import EndpointAuthConfig, HttpEndpoint
        except ImportError:
            logger.warning("alerting: sdk.http недоступен")
            return

        _r = EndpointAuthConfig(required_scopes=["admin.read"])
        _w = EndpointAuthConfig(required_scopes=["admin.write"])
        b  = "/api/v1/plugins/alerting"

        for ep in [
            HttpEndpoint("GET",    f"{b}/rules",                "alerting.list_rules",    "Список правил",              _r),
            HttpEndpoint("POST",   f"{b}/rules",                "alerting.create_rule",   "Создать правило",            _w),
            HttpEndpoint("GET",    f"{b}/rules/{{rule_id}}",    "alerting.get_rule",      "Получить правило",           _r),
            HttpEndpoint("PUT",    f"{b}/rules/{{rule_id}}",    "alerting.update_rule",   "Обновить правило",           _w),
            HttpEndpoint("DELETE", f"{b}/rules/{{rule_id}}",    "alerting.delete_rule",   "Удалить правило",            _w),
            HttpEndpoint("POST",   f"{b}/rules/{{rule_id}}/enable",  "alerting.enable_rule",  "Включить правило",       _w),
            HttpEndpoint("POST",   f"{b}/rules/{{rule_id}}/disable", "alerting.disable_rule", "Отключить правило",      _w),
            HttpEndpoint("GET",    f"{b}/alerts/recent",        "alerting.recent_alerts", "Последние срабатывания",     _r),
        ]:
            self.register_http_endpoint(ep)


# ---------------------------------------------------------------------------

def _safe_format(template: str, **kwargs: Any) -> str:
    """format() без исключений при отсутствующих ключах."""
    try:
        # Собираем только ключи которые есть в шаблоне
        keys = {fn for _, fn, _, _ in Formatter().parse(template) if fn is not None}
        safe = {k: str(kwargs.get(k, f"{{{k}}}")) for k in keys}
        return template.format(**safe)
    except Exception:
        return template
