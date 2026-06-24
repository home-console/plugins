from __future__ import annotations

from typing import Any, Dict
import asyncio

from .operations import poll_and_publish


async def resolve_external_id(plugin: Any, internal_id: str | None, provided_external_id: str | None) -> str | None:
    if provided_external_id:
        return provided_external_id
    if not internal_id:
        return None
    try:
        mappings = await plugin.call_service("devices.list_mappings")
        if isinstance(mappings, list):
            for mapping in mappings:
                if isinstance(mapping, dict) and mapping.get("internal_id") == internal_id:
                    return mapping.get("external_id")
    except Exception:
        pass
    return None


async def ensure_authorization(plugin: Any) -> tuple[bool, bool]:
    from ..oauth_provider import get_status as oauth_get_status, get_cookies as oauth_get_cookies

    oauth_authorized = False
    try:
        status = await oauth_get_status(plugin)
        oauth_authorized = bool(status and status.get("authorized"))
    except Exception:
        pass

    session_cookies = False
    try:
        cookies = await oauth_get_cookies(plugin)
        session_cookies = bool(cookies and isinstance(cookies, dict) and len(cookies) > 0)
    except Exception:
        pass

    return oauth_authorized, session_cookies


async def handle_post_send(
    plugin: Any,
    tasks: set,
    api_client: Any,
    plugin_name: str,
    external_id: str,
    internal_id: str | None,
    params: Dict[str, Any],
    quasar_ws: Any = None,
) -> None:
    """Handle optimistic update, WS check and schedule polling fallback after sending a command."""
    # Optimistic update
    try:
        if isinstance(params, dict) and "on" in params:
            optimistic_reported = {"external_id": external_id, "state": {"on": params["on"]}}
            try:
                await plugin.publish_event("external.device_state_reported", optimistic_reported)
                await plugin.call_service(
                    "logger.log",
                    level="info",
                    message=f"Optimistic state update published for {external_id}: on={params['on']}",
                    plugin=plugin_name,
                    context={"internal_id": internal_id, "external_id": external_id, "state": optimistic_reported["state"]},
                )
            except Exception as pub_err:
                try:
                    await plugin.call_service(
                        "logger.log",
                        level="error",
                        message=f"Failed to publish optimistic state update: {pub_err}",
                        plugin=plugin_name,
                        context={"internal_id": internal_id, "external_id": external_id, "error": str(pub_err)},
                    )
                except Exception:
                    pass
        else:
            try:
                await plugin.call_service(
                    "logger.log",
                    level="debug",
                    message="Optimistic update skipped: params does not contain 'on'",
                    plugin=plugin_name,
                    context={"internal_id": internal_id, "external_id": external_id, "params": params},
                )
            except Exception:
                pass
    except Exception as e:
        try:
            await plugin.call_service(
                "logger.log",
                level="error",
                message=f"Exception in optimistic update logic: {e}",
                plugin=plugin_name,
                context={"internal_id": internal_id, "external_id": external_id, "error": str(e)},
            )
        except Exception:
            pass

    # Check WebSocket activity
    ws_active = False
    if quasar_ws:
        try:
            runner = quasar_ws.runner
            ws_active = runner is not None and not runner.done()
        except Exception:
            pass

    if ws_active:
        try:
            await plugin.call_service(
                "logger.log",
                level="info",
                message=f"Command sent successfully to Yandex device {external_id}. WebSocket active, state will be updated via WebSocket.",
                plugin=plugin_name,
                context={"state": params, "ws_active": True},
            )
        except Exception:
            pass
        return

    # Schedule polling fallback
    try:
        await plugin.call_service(
            "logger.log",
            level="info",
            message=f"Command sent successfully to Yandex device {external_id}. WebSocket not active, using OAuth API polling.",
            plugin=plugin_name,
            context={"state": params, "ws_active": False},
        )
    except Exception:
        pass

    try:
        task = asyncio.create_task(
            poll_and_publish(plugin, api_client, external_id, internal_id, params, plugin_name)
        )
        tasks.add(task)
        task.add_done_callback(lambda t, tasks=tasks: tasks.discard(t))
    except Exception:
        pass
