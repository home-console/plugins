"""
Операции Yandex Smart Home — объявлены и принадлежат плагину yandex_smart_home.

Handlers вызывают ТОЛЬКО сервисы плагина (yandex.sync_devices, yandex.check_devices_online).
Не знают про admin, HTTP, ACL.
"""
from typing import Any, Dict


def register_yandex_operations(plugin: Any) -> None:
    """Регистрирует операции Yandex через SDK-first API плагина (BasePlugin.register_operation_handler)."""

    async def handle_yandex_sync(params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        result = await plugin.call_service("yandex.sync_devices")
        if isinstance(result, list):
            return {"success": True, "devices": result, "count": len(result)}
        return {"success": True, "result": result}

    async def handle_yandex_check_devices_online(params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        result = await plugin.call_service("yandex.check_devices_online")
        if isinstance(result, dict):
            return {"success": True, **result}
        return {"success": True, "result": result}

    try:
        plugin.register_operation_handler("yandex.sync_devices", handle_yandex_sync)
        plugin.register_operation_handler(
            "yandex.check_devices_online", handle_yandex_check_devices_online
        )
    except Exception:
        # Best-effort: отсутствие operations подсистемы не должно ломать старт.
        return
