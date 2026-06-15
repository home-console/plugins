"""
Плагин `yandex_smart_home` — синхронизация реальных устройств Яндекса.

Requires capabilities:
- oauth:yandex — get_access_token, get_status (через фасад oauth_provider)
- yandex:session_cookies — get_cookies для Quasar API (через фасад oauth_provider)

Назначение:
- получить устройства из реального API Яндекса
- преобразовать их в стандартный формат
- опубликовать события об обнаружении устройств
- realtime обновления через Quasar WebSocket

Архитектура:
- plugin-first, in-process
- использует ДВА разных API Яндекса:
  
  1) OAuth API (api.iot.yandex.net):
     - Официальный публичный API
     - Авторизация: OAuth Bearer token (capability oauth:yandex)
     - Используется для: команды, initial sync
  
  2) Quasar API (iot.quasar.yandex.ru):
     ⚠️ КРИТИЧНО: НЕ использует OAuth!
     - Внутренний reverse-engineered API
     - Авторизация: cookies сессии (capability yandex:session_cookies)
     - Используется для: realtime WebSocket обновления

См. QUASAR_ARCHITECTURE_RULE.md для деталей.

Публикует события:
- external.device_discovered для каждого полученного устройства
- external.device_state_reported для realtime обновлений

Ограничения:
- НЕ интегрирует Алису
- НЕ делает refresh token (это задача oauth_yandex)

Комментарии на русском языке.
"""
from __future__ import annotations

import asyncio

from sdk.plugin_ext import BasePlugin, PluginMetadata
from .sync import DeviceSync, DeviceStatusChecker
from .command_handler import CommandHandler
from .yandex_quasar_ws import YandexQuasarWS
from .oauth_provider import get_cookies as oauth_get_cookies


class YandexSmartHomeRealPlugin(BasePlugin):
    """Синхронизирует реальные устройства из API Яндекса.

    Requires capability oauth:yandex (get_access_token, get_status) и
    yandex:session_cookies (get_cookies). Все вызовы — через фасад oauth_provider.

    Публикует события:
    - external.device_discovered для каждого полученного устройства

    Взаимодействует только через:
    - sdk helpers / runtime.api (регистрация сервисов, публикация событий, вызов зависимостей)
    - state_engine / storage (не требуется для первой версии)
    """

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="yandex_smart_home",
            version="0.1.0",
            description="Синхронизация реальных устройств из API Яндекса",
            author="Home Console",
            capabilities_required=["oauth:yandex", "yandex:session_cookies"],
        )

    async def on_load(self) -> None:
        """Загрузка: регистрируем сервисы."""
        await super().on_load()

        # Track background tasks started by this plugin so they can be
        # cancelled on unload to avoid leaked asyncio tasks.
        self._tasks: set = set()

        # Инициализируем модули
        # Передаём self (BasePlugin) как SDK-first facade: call_service/storage_*/publish_event/has_service.
        self.device_sync = DeviceSync(self, self.metadata.name)
        self.device_status_checker = DeviceStatusChecker(self, self.metadata.name)
        self.quasar_ws = YandexQuasarWS(self, self.metadata.name)
        # Передаем quasar_ws в command_handler для проверки активности WebSocket
        self.command_handler = CommandHandler(self, self.metadata.name, self._tasks, self.quasar_ws)

        # Регистрируем сервис синхронизации устройств
        async def _sync_devices():
            """Синхронизировать устройства из реального API Яндекса."""
            return await self._sync_devices_internal()

        await self.register_service("yandex.sync_devices", _sync_devices, admin_only=True)
        
        # Perform initial sync on load
        try:
            await self._sync_devices_internal()
        except Exception as e:
            try:
                await self.call_service(
                    "logger.log",
                    level="warning",
                    message=f"Initial device sync failed: {e}",
                    plugin=self.metadata.name,
                )
            except Exception:
                pass

        # Регистрируем сервис проверки онлайн статуса
        async def _check_devices_online():
            """Проверить онлайн статус всех устройств через Яндекс API."""
            return await self.device_status_checker.check_devices_online()

        await self.register_service("yandex.check_devices_online", _check_devices_online, admin_only=True)

        async def _subscribe_device_updates(device_id: str, callback):
            """Подписка на обновления состояния конкретного устройства (ws)."""
            return self.quasar_ws.subscribe(device_id, callback)

        await self.register_service("yandex.subscribe_device_updates", _subscribe_device_updates, admin_only=True)

        # Start background periodic reconciliation task
        sync_task = asyncio.create_task(self._periodic_sync_loop())
        self._tasks.add(sync_task)
        sync_task.add_done_callback(lambda t, tasks=self._tasks: tasks.discard(t))

    async def on_start(self) -> None:
        """Запуск: регистрируем операции, логируем и подписываемся на события."""
        await super().on_start()

        from .operations import register_yandex_operations
        register_yandex_operations(self)

        try:
            await self.call_service(
                "logger.log",
                level="info",
                message="yandex_smart_home запущен",
                plugin=self.metadata.name,
            )
        except Exception:
            pass

        # Подписаться на внутренние запросы команд от DevicesModule
        async def _internal_command_handler(event_type: str, data: dict):
            """Обработчик внутренних команд управления устройствами."""
            await self.command_handler.handle_command(data)

        # Сохранить хендлер и подписаться
        self._internal_command_handler = _internal_command_handler
        try:
            await self.subscribe_event("internal.device_command_requested", self._internal_command_handler)
        except Exception:
            pass

        # Подписываемся на событие успешной device-авторизации
        async def _on_device_auth_linked(event_type: str, data: dict):
            """Обработчик события yandex.device_auth.linked: синхронизация устройств + Quasar WS."""
            try:
                if not data.get("quasar_ready"):
                    return
                await self.call_service(
                    "logger.log",
                    level="info",
                    message="Device auth linked, syncing devices and starting Quasar WS",
                    plugin=self.metadata.name,
                )
                # Включаем реальный API после успешного device auth (нужно для sync_devices)
                await self.storage_set("yandex", "use_real_api", {"enabled": True})
                
                # Синхронизация устройств и автомаппинг
                try:
                    result = await self._sync_devices_internal()
                    await self.call_service(
                        "logger.log",
                        level="info",
                        message=f"Device auth linked: synced {result.get('synced', 0)} devices, mapped {result.get('mapped', 0)}",
                        plugin=self.metadata.name,
                    )
                except Exception as sync_err:
                    await self.call_service(
                        "logger.log",
                        level="warning",
                        message=f"Device sync after auth failed (Quasar WS will still run): {sync_err}",
                        plugin=self.metadata.name,
                    )
                # Realtime-обновления через Quasar WebSocket
                await self.quasar_ws.start()
                runner = self.quasar_ws.runner
                if runner:
                    self._tasks.add(runner)
                    runner.add_done_callback(lambda t, tasks=self._tasks: tasks.discard(t))
            except Exception as e:
                await self.call_service(
                    "logger.log",
                    level="error",
                    message=f"Failed after device auth (sync/Quasar WS): {e}",
                    plugin=self.metadata.name,
                )

        self._device_auth_handler = _on_device_auth_linked
        try:
            await self.subscribe_event("yandex.device_auth.linked", self._device_auth_handler)
        except Exception:
            pass

        # Запустить realtime-поток Quasar, если включён реальный API и есть cookies
        try:
            if await self._is_real_api_enabled():
                # Проверяем наличие cookies (либо из device_auth, либо из oauth)
                cookies = await self._get_cookies()
                if cookies:
                    await self.quasar_ws.start()
                    runner = self.quasar_ws.runner
                    if runner:
                        self._tasks.add(runner)
                        runner.add_done_callback(lambda t, tasks=self._tasks: tasks.discard(t))
                else:
                    await self.call_service(
                        "logger.log",
                        level="warning",
                        message="Quasar WS not started: cookies not found. Use device auth or OAuth with cookies.",
                        plugin=self.metadata.name,
                    )
        except Exception:
            pass

    async def _get_cookies(self):
        """Получить cookies через capability yandex:session_cookies (фасад oauth_provider)."""
        return await oauth_get_cookies(self)

    async def on_stop(self) -> None:
        """Остановка: логируем завершение."""
        await super().on_stop()

        try:
            await self.call_service(
                "logger.log",
                level="info",
                message="yandex_smart_home остановлен",
                plugin=self.metadata.name,
            )
        except Exception:
            pass

        try:
            await self.quasar_ws.stop()
        except Exception:
            pass

    async def on_unload(self) -> None:
        """Выгрузка: удаляем сервисы и отменяем фоновые задачи."""
        await super().on_unload()

        # Отписываемся от событий
        try:
            if hasattr(self, '_device_auth_handler'):
                await self.unsubscribe_event("yandex.device_auth.linked", self._device_auth_handler)
        except Exception:
            pass

        # Cancel background tasks started by this plugin
        try:
            tasks = getattr(self, "_tasks", None)
            if tasks:
                # cancel
                for t in list(tasks):
                    try:
                        t.cancel()
                    except Exception:
                        pass

                # wait for completion with timeout
                try:
                    await asyncio.wait_for(asyncio.gather(*list(tasks), return_exceptions=True), timeout=2.0)
                except asyncio.TimeoutError:
                    # timed out waiting — tasks may still be running, but we've attempted cancel
                    pass
                except Exception:
                    # ignore other errors from tasks
                    pass

                # suppress CancelledError and clear tracking
                for t in list(tasks):
                    try:
                        if not t.done():
                            t.cancel()
                    except Exception:
                        pass
                try:
                    tasks.clear()
                except Exception:
                    pass

        except Exception:
            pass

        try:
            await self.unregister_service("yandex.sync_devices")
        except Exception:
            pass

        try:
            await self.unregister_service("yandex.check_devices_online")
        except Exception:
            pass

        try:
            await self.unregister_service("yandex.subscribe_device_updates")
        except Exception:
            pass

        try:
            await self.quasar_ws.stop()
        except Exception:
            pass

    async def _sync_devices_internal(self) -> dict:
        """Internal method: sync devices and auto-map external (own provider) to internal devices."""
        result = {
            "synced": 0,
            "mapped": 0,
        }
        
        try:
            devices = await self.device_sync.sync_devices()
            result["synced"] = len(devices) if devices else 0
            
            try:
                # devices.auto_map_own — self-service версия auto_map_external без
                # admin_only: разрешена этому плагину через allowed_services в манифесте
                # и маппит только устройства provider="yandex" (свои собственные).
                map_result = await self.call_service(
                    "devices.auto_map_own",
                    provider="yandex",
                )
                if isinstance(map_result, dict):
                    result["mapped"] = map_result.get("created", 0)
            except Exception as map_err:
                # Log mapping error but don't fail entire sync
                try:
                    await self.call_service(
                        "logger.log",
                        level="warning",
                        message=f"Device auto-mapping failed: {map_err}",
                        plugin=self.metadata.name,
                    )
                except Exception:
                    pass
            
            return result
        except Exception as e:
            # Log sync error but don't re-raise
            try:
                await self.call_service(
                    "logger.log",
                    level="error",
                    message=f"Device sync failed: {e}",
                    plugin=self.metadata.name,
                )
            except Exception:
                pass
            return result
    
    async def _periodic_sync_loop(self) -> None:
        """Background task: periodically reconcile devices every 300 seconds."""
        while True:
            try:
                await asyncio.sleep(300)
                # Safety reconciliation: sync devices every 5 minutes
                try:
                    await self._sync_devices_internal()
                except Exception as e:
                    # Log reconciliation error but don't crash the loop
                    try:
                        await self.call_service(
                            "logger.log",
                            level="warning",
                            message=f"Periodic reconciliation failed: {e}",
                            plugin=self.metadata.name,
                        )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                # Task was cancelled by plugin unload
                break
            except Exception:
                # Unexpected error - sleep and retry
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    break

    async def _is_real_api_enabled(self) -> bool:
        """Проверка feature-флага использования реального API."""
        try:
            use_real_data = await self.storage_get("yandex", "use_real_api")
            if isinstance(use_real_data, dict):
                return bool(use_real_data.get("enabled", False))
            return bool(use_real_data)
        except Exception:
            return False
