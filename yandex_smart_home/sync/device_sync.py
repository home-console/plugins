"""
Модуль для синхронизации устройств из Яндекс API.

Обеспечивает получение устройств из API и публикацию событий об их обнаружении.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from ..clients.api_client import YandexAPIClient
from ..transformers.device_transformer import DeviceTransformer


class DeviceSync:
    """Класс для синхронизации устройств."""

    def __init__(self, plugin: Any, plugin_name: str):
        """Инициализация синхронизатора.

        Args:
            plugin: SDK-first facade (BasePlugin)
            plugin_name: имя плагина для логирования
        """
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.api_client = YandexAPIClient(plugin, plugin_name)

    async def sync_devices(self) -> List[Dict[str, Any]]:
        """Синхронизировать устройства из реального API Яндекса.

        Этапы:
        1. Проверить feature flag `yandex.use_real_api`
        2. Получить токены через capability oauth:yandex (фасад oauth_provider)
        3. Выполнить HTTP GET к https://api.iot.yandex.net/v1.0/user/info
        4. Преобразовать каждое устройство в стандартный формат
        5. Опубликовать external.device_discovered для каждого
        6. Вернуть список преобразованных устройств

        Returns:
            Список устройств в стандартном формате

        Raises:
            RuntimeError: если токены недоступны или запрос к API не удался
        """
        # Проверяем feature-флаг ДО открытия operation-записи, чтобы не порождать
        # ERROR-операции которые OperationsWorker будет бесконечно ретраить.
        try:
            use_real_data = await self.plugin.storage_get("yandex", "use_real_api")
            if isinstance(use_real_data, dict):
                use_real = use_real_data.get("enabled", False) if use_real_data else False
            else:
                use_real = bool(use_real_data)
        except Exception:
            use_real = False

        if not use_real:
            # Real API не настроен — тихий ранний выход без создания operation-записи.
            return []

        async with self.plugin.context.operation_context.operation(
            "yandex.sync_devices", self.plugin_name
        ):
            return await self._sync_devices_impl()

    async def _sync_devices_impl(self) -> List[Dict[str, Any]]:
        """Реализация синхронизации устройств (вызывается только когда real API включён)."""

        # Пытаемся получить устройства через Quasar API (с домами/комнатами)
        # Если не получается, fallback на OAuth API
        yandex_devices = []
        households = []
        
        try:
            # Пробуем Quasar API (требует cookies)
            quasar_response = await self.api_client.get_quasar_devices()
            
            # Структура Quasar API: {"status": "ok", "households": [...]}
            if isinstance(quasar_response, dict) and quasar_response.get("status") == "ok":
                households = quasar_response.get("households", [])
                
                # Извлекаем все устройства из всех домов
                for house in households:
                    house_id = house.get("id")
                    house_name = house.get("name")
                    all_devices = house.get("all", [])
                    
                    # Добавляем информацию о доме к каждому устройству
                    for device in all_devices:
                        device["house_id"] = house_id
                        device["house_name"] = house_name
                        yandex_devices.append(device)
                
                # Логируем успех
                try:
                    await self.plugin.call_service(
                        "logger.log",
                        level="info",
                        message=f"Loaded {len(yandex_devices)} devices from Quasar API ({len(households)} households)",
                        plugin=self.plugin_name,
                    )
                except Exception:
                    pass
        except Exception as e:
            # Quasar API не доступен (нет cookies или ошибка) - fallback на OAuth API
            try:
                await self.plugin.call_service(
                    "logger.log",
                    level="warning",
                    message=f"Quasar API unavailable, falling back to OAuth API: {e}",
                    plugin=self.plugin_name,
                )
            except Exception:
                pass
            
            # Fallback: используем OAuth API
            api_response = await self.api_client.get_user_info()
            
            # Структура ответа OAuth API: {"devices": [...]}
            if isinstance(api_response, dict) and "devices" in api_response:
                yandex_devices = api_response.get("devices", [])
            elif isinstance(api_response, list):
                yandex_devices = api_response

        # Преобразовать устройства и опубликовать события
        devices = []
        sync_timestamp = time.time()

        # DEBUG: Save full households hierarchy
        try:
            if households:
                await self.plugin.storage_set(
                    "yandex_debug_households",
                    f"hierarchy_{int(sync_timestamp * 1000)}",
                    {
                        "timestamp": sync_timestamp,
                        "household_count": len(households),
                        "households": households,
                    },
                )
            
            # DEBUG: Save individual houses with their metadata
            for house in households:
                house_id = house.get("id")
                house_name = house.get("name")
                rooms = house.get("rooms", [])
                
                if house_id:
                    await self.plugin.storage_set(
                        "yandex_debug_houses",
                        f"{house_id}_{int(sync_timestamp * 1000)}",
                        {
                            "timestamp": sync_timestamp,
                            "house_id": house_id,
                            "house_name": house_name,
                            "room_count": len(rooms),
                            "rooms": rooms,
                            "full_house_data": house,
                        },
                    )
                
                # DEBUG: Save rooms information
                for room in rooms:
                    room_id = room.get("id")
                    room_name = room.get("name")
                    
                    if room_id:
                        await self.plugin.storage_set(
                            "yandex_debug_rooms",
                            f"{house_id}_{room_id}_{int(sync_timestamp * 1000)}",
                            {
                                "timestamp": sync_timestamp,
                                "house_id": house_id,
                                "house_name": house_name,
                                "room_id": room_id,
                                "room_name": room_name,
                                "room_data": room,
                            },
                        )
        except Exception:
            pass

        for yandex_device in yandex_devices:
            # Преобразуем устройство в стандартный формат
            device = DeviceTransformer.transform_device(yandex_device)

            if device:
                devices.append(device)
                
                # DEBUG: Save external mapping (internal_id <-> external_id)
                try:
                    internal_id = device.get("id")
                    external_id = device.get("external_id")
                    house_id = yandex_device.get("house_id")
                    house_name = yandex_device.get("house_name")
                    room_name = yandex_device.get("room_name")
                    device_type = yandex_device.get("type")
                    device_name = yandex_device.get("name")
                    
                    if internal_id and external_id:
                        await self.plugin.storage_set(
                            "yandex_debug_external_mapping",
                            f"{internal_id}_{int(sync_timestamp * 1000)}",
                            {
                                "timestamp": sync_timestamp,
                                "internal_id": internal_id,
                                "external_id": external_id,
                                "house_id": house_id,
                                "house_name": house_name,
                                "room_name": room_name,
                                "device_name": device_name,
                                "device_type": device_type,
                                "capabilities_count": len(device.get("capabilities", [])),
                                "properties_count": len(device.get("properties", [])),
                                "has_state": bool(device.get("state")),
                                "online": device.get("online"),
                            },
                        )
                        
                        # DEBUG: Save extracted state from capabilities and properties
                        try:
                            extracted_state = device.get("state", {})
                            capabilities = device.get("capabilities", [])
                            properties = device.get("properties", [])
                            
                            # Count how much we extracted from each source
                            state_keys = list(extracted_state.keys())
                            
                            # Analyze what came from capabilities vs properties
                            capabilities_with_state = sum(1 for cap in yandex_device.get("capabilities", []) 
                                                          if cap.get("state", {}).get("value") is not None)
                            properties_with_state = sum(1 for prop in yandex_device.get("properties", []) 
                                                       if prop.get("state", {}).get("value") is not None)
                            
                            await self.plugin.storage_set(
                                "yandex_debug_state_extraction",
                                f"{internal_id}_{int(sync_timestamp * 1000)}",
                                {
                                    "timestamp": sync_timestamp,
                                    "internal_id": internal_id,
                                    "external_id": external_id,
                                    "device_name": device_name,
                                    "device_type": device_type,
                                    "extracted_state": extracted_state,
                                    "state_keys_count": len(state_keys),
                                    "state_keys": state_keys,
                                    "capabilities": {
                                        "total": len(capabilities),
                                        "with_state_value": capabilities_with_state,
                                        "list": capabilities,
                                    },
                                    "properties": {
                                        "total": len(properties),
                                        "with_state_value": properties_with_state,
                                        "list": properties,
                                    },
                                    "raw_capability_data": yandex_device.get("capabilities", []),
                                    "raw_property_data": yandex_device.get("properties", []),
                                },
                            )
                        except Exception:
                            pass
                except Exception:
                    pass
                
                # DEBUG 1: Log raw REST snapshot
                try:
                    ext_id = device.get("external_id")
                    await self.plugin.call_service(
                        "logger.log",
                        level="debug",
                        message=f"[REST_SNAPSHOT] Device from API",
                        context={
                            "external_id": ext_id,
                            "has_state": bool(device.get("state")),
                            "has_capabilities": bool(device.get("capabilities")),
                            "device_type": device.get("device_type"),
                        },
                    )
                    
                    # Save raw snapshot to debug namespace
                    await self.plugin.storage_set(
                        "yandex_debug_rest",
                        f"{ext_id}_{int(sync_timestamp * 1000)}",
                        {
                            "timestamp": sync_timestamp,
                            "external_id": ext_id,
                            "raw_device": yandex_device,
                        },
                    )
                except Exception:
                    pass

                # Публикуем событие обнаружения (КРИТИЧЕСКИ: именно это событие)
                try:
                    from sdk.events import ExternalDeviceDiscoveredPayload

                    payload: ExternalDeviceDiscoveredPayload = device
                    await self.plugin.publish_event(
                        "external.device_discovered",
                        payload
                    )
                    
                    # Если есть состояние — публикуем immediate snapshot для reconciliation,
                    # но не перезаписываем более свежий WS state.
                    if device.get("state"):
                        ext_id = device.get("external_id")
                        parsed_state = device.get("state")

                        # Проверяем, не приходили ли недавние WS-обновления для этого устройства
                        should_publish_state = True
                        try:
                            mapping = await self.plugin.storage_get("devices_mappings", ext_id)
                            internal_id = mapping.get("internal_id") if isinstance(mapping, dict) else None
                            if internal_id:
                                internal_device = await self.plugin.storage_get("devices", internal_id)
                                if isinstance(internal_device, dict):
                                    last_ws_update = internal_device.get("last_ws_update")
                                    if isinstance(last_ws_update, (int, float)):
                                        now_ts = time.time()
                                        # N секунд «свежести» WS перед тем, как REST может перезаписать reported
                                        REST_STATE_GRACE_SEC = 30
                                        if (now_ts - last_ws_update) <= REST_STATE_GRACE_SEC:
                                            should_publish_state = False
                        except Exception:
                            # В случае ошибок безопасности предпочитаем публиковать snapshot,
                            # чтобы не нарушить существующее поведение.
                            should_publish_state = True

                        if should_publish_state:
                            # DEBUG: log parsed state before publishing
                            try:
                                await self.plugin.call_service(
                                    "logger.log",
                                    level="debug",
                                    message=f"[device_sync] Publishing initial state snapshot",
                                    plugin=self.plugin_name,
                                    context={
                                        "external_id": ext_id,
                                        "state": parsed_state,
                                    },
                                )
                            except Exception:
                                pass
                            await self.plugin.publish_event(
                                "external.device_state_reported",
                                {
                                    "external_id": ext_id,
                                    "state": parsed_state,
                                    "source": "rest",
                                },
                            )
                except Exception as e:
                    # Ошибка публикации одного устройства не должна блокировать остальные
                    # Логируем и продолжаем
                    try:
                        await self.plugin.call_service(
                            "logger.log",
                            level="warning",
                            message=f"Ошибка публикации события для устройства {device.get('external_id')}: {e}",
                            plugin=self.plugin_name,
                        )
                    except Exception:
                        pass
        
        # DEBUG 1B: Save full REST snapshot
        try:
            if devices:
                await self.plugin.storage_set(
                    "yandex_debug_rest_full",
                    f"sync_{int(sync_timestamp * 1000)}",
                    {
                        "timestamp": sync_timestamp,
                        "device_count": len(devices),
                        "devices": devices,
                    },
                )
            
            # DEBUG: Save Yandex provider metadata (all houses + devices summary)
            provider_summary = {
                "timestamp": sync_timestamp,
                "total_households": len(households),
                "total_devices": len(yandex_devices),
                "total_transformed_devices": len(devices),
                "houses": [],
                "devices_by_type": {},
                "devices_by_status": {},
                "capabilities_summary": {},
                "properties_summary": {},
            }
            
            # Build provider summary
            for house in households:
                house_entry = {
                    "id": house.get("id"),
                    "name": house.get("name"),
                    "room_count": len(house.get("rooms", [])),
                    "device_count": len(house.get("all", [])),
                }
                provider_summary["houses"].append(house_entry)
            
            # Count devices by type
            for device in devices:
                device_type = device.get("device_type", "unknown")
                if device_type not in provider_summary["devices_by_type"]:
                    provider_summary["devices_by_type"][device_type] = 0
                provider_summary["devices_by_type"][device_type] += 1
                
                # Count by status
                online = device.get("online", False)
                status = "online" if online else "offline"
                if status not in provider_summary["devices_by_status"]:
                    provider_summary["devices_by_status"][status] = 0
                provider_summary["devices_by_status"][status] += 1
                
                # Count capabilities
                for cap in device.get("capabilities", []):
                    cap_type = cap.get("type", "unknown")
                    if cap_type not in provider_summary["capabilities_summary"]:
                        provider_summary["capabilities_summary"][cap_type] = 0
                    provider_summary["capabilities_summary"][cap_type] += 1
                
                # Count properties
                for prop in device.get("properties", []):
                    prop_type = prop.get("type", "unknown")
                    if prop_type not in provider_summary["properties_summary"]:
                        provider_summary["properties_summary"][prop_type] = 0
                    provider_summary["properties_summary"][prop_type] += 1
            
            await self.plugin.storage_set(
                "yandex_debug_provider_meta",
                f"summary_{int(sync_timestamp * 1000)}",
                provider_summary,
            )
        except Exception:
            pass

        return devices
