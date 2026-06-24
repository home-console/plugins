"""Shared command-related operations extracted from CommandHandler."""
from __future__ import annotations

from typing import Any, Dict
import asyncio

from ..transformers.device_transformer import DeviceTransformer


async def poll_and_publish(
    plugin: Any,
    api_client: Any,
    external_id: str,
    internal_id: str | None,
    params: Dict[str, Any],
    plugin_name: str,
) -> None:
    """Polling fallback: try to fetch device state and publish reported state.

    This is a near-copy of the original CommandHandler._poll_and_publish logic
    adjusted to operate on passed-in dependencies.
    """
    await asyncio.sleep(0.8)

    state_updated = False

    try:
        devices_list_response = await api_client.get_devices_list()
        devices_list = devices_list_response.get("devices") or []

        target = None
        for d in devices_list:
            if d.get("id") == external_id:
                target = d
                break

        if target is not None:
            # Используем новый метод для извлечения capabilities и properties
            caps_list, cap_state = DeviceTransformer._extract_capabilities(target.get("capabilities", []))
            prop_list, prop_state = DeviceTransformer._extract_properties(target.get("properties", []))
            
            # Объединяем состояния
            state = {**cap_state, **prop_state}
            
            # Также учитываем states если они есть
            raw_states = target.get("states") or []
            if raw_states:
                old_state = DeviceTransformer._extract_state(raw_states, None)
                state.update(old_state)
            
            reported = {"external_id": external_id, "state": {}}
            if isinstance(state, dict) and "on" in state:
                reported["state"]["on"] = state["on"]

            if reported["state"]:
                try:
                    await plugin.publish_event("external.device_state_reported", reported)
                    state_updated = True
                except Exception:
                    try:
                        await plugin.call_service(
                            "logger.log",
                            level="warning",
                            message=f"Failed to publish external.device_state_reported after poll for {external_id}",
                            plugin=plugin_name,
                        )
                    except Exception:
                        pass
    except Exception:
        try:
            await plugin.call_service(
                "logger.log",
                level="error",
                message=f"Unexpected error in poll task for {external_id}",
                plugin=plugin_name,
            )
        except Exception:
            pass

    if not state_updated:
        try:
            device = await plugin.call_service("devices.get", internal_id)
            if isinstance(device, dict):
                device_state = device.get("state", {})
                if isinstance(device_state, dict) and device_state.get("pending") is True:
                    desired = device_state.get("desired", {})
                    if isinstance(desired, dict) and "on" in desired:
                        optimistic_reported = {"external_id": external_id, "state": {"on": desired["on"]}}
                        try:
                            await plugin.publish_event("external.device_state_reported", optimistic_reported)
                            await plugin.call_service(
                                "logger.log",
                                level="info",
                                message=f"Optimistic state update for {external_id} (polling did not return device state)",
                                plugin=plugin_name,
                            )
                        except Exception:
                            pass
        except Exception:
            pass


async def reset_pending_on_error(
    plugin: Any,
    internal_id: str | None,
    external_id: str | None,
    error_reason: str,
    plugin_name: str,
) -> None:
    """Reset pending flag for device when command delivery failed.

    Adapted from CommandHandler._reset_pending_on_error.
    """
    if not internal_id:
        try:
            await plugin.call_service(
                "logger.log",
                level="debug",
                message=f"_reset_pending_on_error: no internal_id provided",
                plugin=plugin_name,
            )
        except Exception:
            pass
        return

    try:
        import time

        device = await plugin.call_service("devices.get", internal_id)
        if not isinstance(device, dict):
            try:
                await plugin.call_service(
                    "logger.log",
                    level="debug",
                    message=f"_reset_pending_on_error: device {internal_id} not found or invalid",
                    plugin=plugin_name,
                )
            except Exception:
                pass
            return

        device_state = device.get("state", {})
        if not isinstance(device_state, dict) or device_state.get("pending") is not True:
            try:
                await plugin.call_service(
                    "logger.log",
                    level="debug",
                    message=f"_reset_pending_on_error: device {internal_id} not in pending state",
                    plugin=plugin_name,
                    context={"pending": device_state.get("pending")},
                )
            except Exception:
                pass
            return

        device_state["pending"] = False
        device["state"] = device_state
        device["updated_at"] = time.time()

        await plugin.call_service(
            "devices.update_device_fields",
            internal_id,
            {
                "state": device_state,
                "updated_at": device["updated_at"]
            }
        )

        try:
            await plugin.call_service(
                "logger.log",
                level="info",
                message=f"Reset pending state for device {internal_id} ({external_id}): {error_reason}",
                plugin=plugin_name,
                context={"desired": device_state.get("desired"), "reported": device_state.get("reported")},
            )
        except Exception:
            pass
    except Exception as e:
        try:
            await plugin.call_service(
                "logger.log",
                level="error",
                message=f"_reset_pending_on_error failed for {internal_id}: {e}",
                plugin=plugin_name,
            )
        except Exception:
            pass
