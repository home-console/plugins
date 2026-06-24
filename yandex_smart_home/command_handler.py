"""
Модуль для обработки команд управления устройствами через Яндекс API.
Использует capability oauth:yandex через фасад oauth_provider (get_status).
"""
from __future__ import annotations

from typing import Any, Dict

from .clients import YandexAPIClient
from .transformers.device_transformer import DeviceTransformer
from .oauth_provider import get_status as oauth_get_status, get_cookies as oauth_get_cookies


class CommandHandler:
    """Класс для обработки команд управления устройствами."""

    def __init__(self, plugin: Any, plugin_name: str, tasks: set, quasar_ws: Any = None):
        """Инициализация обработчика команд.

        Args:
            plugin: SDK-first facade (BasePlugin)
            plugin_name: имя плагина для логирования
            tasks: множество для отслеживания фоновых задач
            quasar_ws: экземпляр YandexQuasarWS для проверки активности WebSocket
        """
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.tasks = tasks
        self.api_client = YandexAPIClient(plugin, plugin_name)
        self.quasar_ws = quasar_ws  # WebSocket клиент для проверки активности

    async def _log(self, level: str, message: str, context: dict | None = None) -> None:
        """Helper to log via plugin.call_service, swallowing errors.

        Use this to reduce repetitive try/except around logger calls.
        """
        try:
            await self.plugin.call_service(
                "logger.log",
                level=level,
                message=message,
                plugin=self.plugin_name,
                context=context or {},
            )
        except Exception:
            pass
    async def _resolve_external_id(self, internal_id: str | None, provided_external_id: str | None) -> str | None:
        """Resolve external_id: prefer provided value, otherwise lookup mappings for internal_id."""
        if provided_external_id:
            return provided_external_id
        if not internal_id:
            return None
        try:
            mappings = await self.plugin.call_service("devices.list_mappings")
            if isinstance(mappings, list):
                for mapping in mappings:
                    if isinstance(mapping, dict) and mapping.get("internal_id") == internal_id:
                        return mapping.get("external_id")
        except Exception:
            pass
        return None

    async def _ensure_authorization(self) -> tuple[bool, bool]:
        """Return (oauth_authorized, session_cookies) flags.

        Encapsulates calls to oauth provider and swallows exceptions.
        """
        oauth_authorized = False
        try:
            status = await oauth_get_status(self.plugin)
            oauth_authorized = bool(status and status.get("authorized"))
        except Exception:
            pass

        session_cookies = False
        try:
            cookies = await oauth_get_cookies(self.plugin)
            session_cookies = bool(cookies and isinstance(cookies, dict) and len(cookies) > 0)
        except Exception:
            pass

        return oauth_authorized, session_cookies
    async def handle_command(self, data: Dict[str, Any]) -> None:
        """Обработать команду управления устройством.

        Ожидаемый формат payload'а:
        {
            "internal_id": "...",
            "external_id": "...",
            "command": "set_state",
            "params": { ... }
        }

        Args:
            data: данные команды
        """
        async with self.plugin.context.operation_context.operation(
            "yandex.send_device_command", self.plugin_name
        ):
            await self._log(
                "debug",
                "yandex_smart_home: received internal.device_command_requested",
                {"data": data},
            )

            external_id = data.get("external_id")
            params = data.get("params", {}) or {}

            # Попробовать разрешить external_id (переданный или по mapping)
            internal_id = data.get("internal_id")
            external_id = await self._resolve_external_id(internal_id, external_id)

            if not external_id:
                # Нечем управлять - сбрасываем pending
                await self._log(
                    "warning",
                    f"internal.device_command_requested missing external_id: {data}",
                )
                # Сбрасываем pending при отсутствии external_id
                await self._reset_pending_on_error(data.get("internal_id"), None, "Missing external_id")
                return

            # Авторизация: проверка OAuth и cookies в helper'e
            oauth_authorized, session_cookies = await self._ensure_authorization()

            if not oauth_authorized and not session_cookies:
                await self._log(
                    "warning",
                    f"Yandex not authorized (no OAuth, no cookies), cannot send command for {external_id}",
                )
                await self._reset_pending_on_error(internal_id, external_id, "Yandex not authorized")
                return

            use_quasar = session_cookies and not oauth_authorized

            # Конвертируем params в действия по Яндекс API
            actions = DeviceTransformer.convert_params_to_actions(params)

            await self._log(
                "info",
                f"Sending command to Yandex device (quasar={use_quasar})",
                {"device_id": external_id, "internal_id": internal_id, "params": params, "actions": actions},
            )

            try:
                # Delegate send logic to commands.send.send_command
                from .commands.send import send_command

                await send_command(
                    self.plugin,
                    self.api_client,
                    self.plugin_name,
                    external_id,
                    actions,
                    use_quasar,
                )

                # After successful send: optimistic update + ws check + schedule poll
                from .commands.flow import handle_post_send

                await handle_post_send(
                    self.plugin,
                    self.tasks,
                    self.api_client,
                    self.plugin_name,
                    external_id,
                    data.get("internal_id"),
                    params,
                    self.quasar_ws,
                )

            except RuntimeError as e:
                # Ошибка от API — сбрасываем pending и логируем ошибку
                error_msg = str(e)
                await self._log(
                    "error",
                    f"Yandex API error: {error_msg}",
                    {"device_id": external_id, "error": error_msg},
                )

                # Сбрасываем pending при ошибке API
                await self._reset_pending_on_error(data.get("internal_id"), external_id, f"API error: {error_msg}")

            except Exception as e:
                # Прочие ошибки — сбрасываем pending
                await self._log(
                    "error",
                    f"Error sending command to Yandex device {external_id}: {type(e).__name__}: {e}",
                )

                # Сбрасываем pending при прочих ошибках
                await self._reset_pending_on_error(data.get("internal_id"), external_id, f"Error: {type(e).__name__}")

    async def _poll_and_publish(self, external_id: str, internal_id: str | None, params: Dict[str, Any]) -> None:
        # Delegate to commands.operations.poll_and_publish
        from .commands.operations import poll_and_publish

        await poll_and_publish(
            self.plugin,
            self.api_client,
            external_id,
            internal_id,
            params,
            self.plugin_name,
        )

    async def _reset_pending_on_error(
        self, internal_id: str | None, external_id: str | None, error_reason: str
    ) -> None:
        """Сбросить pending состояние устройства при ошибке отправки команды.

        Args:
            internal_id: внутренний ID устройства
            external_id: внешний ID устройства
            error_reason: причина ошибки для логирования
        """
        # Delegate to commands.operations.reset_pending_on_error
        from .commands.operations import reset_pending_on_error

        await reset_pending_on_error(
            self.plugin,
            internal_id,
            external_id,
            error_reason,
            self.plugin_name,
        )
