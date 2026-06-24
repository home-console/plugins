"""Send helpers for device commands.

Centralizes sending via OAuth API or Quasar cookies and logging.
"""
from __future__ import annotations

from typing import Any, Dict


async def send_command(
    plugin: Any,
    api_client: Any,
    plugin_name: str,
    external_id: str,
    actions: Dict[str, Any],
    use_quasar: bool,
) -> None:
    """Send actions to device via api_client.

    Raises RuntimeError on API errors to preserve existing handling.
    """
    try:
        if use_quasar:
            try:
                await plugin.call_service(
                    "logger.log",
                    level="info",
                    message=f"Sending command via Quasar (cookies) for {external_id}",
                    plugin=plugin_name,
                )
            except Exception:
                pass
            await api_client.send_device_action_quasar(external_id, actions)
        else:
            await api_client.send_device_action(external_id, actions)
    except RuntimeError:
        # Propagate runtime errors from api_client for upstream handling
        raise
    except Exception as e:
        try:
            await plugin.call_service(
                "logger.log",
                level="error",
                message=f"Failed to send command to {external_id}: {e}",
                plugin=plugin_name,
            )
        except Exception:
            pass
        raise
