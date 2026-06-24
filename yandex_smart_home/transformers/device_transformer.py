"""
Модуль для трансформации устройств из формата Яндекс API в стандартный формат.

Преобразует устройства из ответа Яндекса в формат, используемый в Home Console.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class DeviceTransformer:
    """Класс для трансформации устройств Яндекс API."""

    @staticmethod
    def transform_device(yandex_device: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            device_id = yandex_device.get("id")
            if not device_id:
                return None

            name = yandex_device.get("name") or yandex_device.get("title") or device_id
            yandex_type = yandex_device.get("type", "")
            device_type = DeviceTransformer._extract_device_type(yandex_type)

            # КРИТИЧНО: Извлекаем capabilities с их СОСТОЯНИЯМИ (state.value)
            yandex_capabilities = yandex_device.get("capabilities", [])
            capabilities, device_state = DeviceTransformer._extract_capabilities(yandex_capabilities)

            # КРИТИЧНО: Извлекаем properties с их ЗНАЧЕНИЯМИ (state.value)
            yandex_properties = yandex_device.get("properties", [])
            properties, properties_state = DeviceTransformer._extract_properties(yandex_properties)
            
            # Объединяем состояния: сначала capabilities, потом properties (properties overrides)
            device_state.update(properties_state)

            home_id = yandex_device.get("house_id")
            home_name = yandex_device.get("house_name")
            room_id = yandex_device.get("room_id")
            room_name = yandex_device.get("room_name")
            if not room_name:
                parameters = yandex_device.get("parameters", {})
                if isinstance(parameters, dict):
                    room_name = parameters.get("room_name")

            device_state_value = yandex_device.get("state")
            online = device_state_value not in ("offline", None) if device_state_value else True

            # URL иконки Яндекса по типу устройства (devices.types.hub → .../icons-devices-devices.types.hub.svg/orig)
            icon_url = None
            if yandex_type:
                icon_url = f"https://avatars.mds.yandex.net/get-iot/icons-devices-{yandex_type}.svg/orig"

            device = {
                "provider": "yandex",
                "external_id": device_id,
                "name": name,
                "type": device_type,
                "capabilities": capabilities,
                "properties": properties,
                "state": device_state,
            }

            if icon_url:
                device["icon_url"] = icon_url
            if home_id:
                device["home_id"] = home_id
            if home_name:
                device["home_name"] = home_name
            if room_id:
                device["room_id"] = room_id
            if room_name:
                device["room_name"] = room_name
            if device_state_value is not None:
                device["online"] = online

            return device
        except Exception:
            return None

    @staticmethod
    def _extract_device_type(yandex_type: str) -> str:
        if not yandex_type:
            return "unknown"
        parts = yandex_type.split(".")
        if parts:
            return parts[-1]
        return "unknown"

    @staticmethod
    def _extract_capabilities(yandex_capabilities: list) -> tuple[list, dict]:
        """Извлекает capabilities и их СОСТОЯНИЯ (state.value).
        
        Returns:
            Tuple[capabilities_list, state_dict]
            - capabilities_list: список типов capabilities
            - state_dict: словарь с extracted state values
        """
        capabilities = []
        state = {}
        
        for cap in yandex_capabilities:
            cap_type = cap.get("type", "")
            if not cap_type:
                continue
            
            # Извлекаем простое имя capability
            parts = cap_type.split(".")
            simple_name = parts[-1] if parts else ""
            if simple_name:
                capabilities.append(simple_name)
            
            # КРИТИЧНО: Извлекаем СОСТОЯНИЕ из capability (state.value + instance)
            cap_state = cap.get("state", {})
            if isinstance(cap_state, dict):
                value = cap_state.get("value")
                instance = cap_state.get("instance", simple_name)
                
                if value is not None:
                    # Нормализуем on_off
                    if simple_name == "on_off" or cap_type == "devices.capabilities.on_off":
                        if isinstance(value, bool):
                            state["on"] = value
                        elif isinstance(value, str):
                            v = value.strip().lower()
                            state["on"] = v in ("on", "true", "1", "yes")
                        elif isinstance(value, (int, float)):
                            state["on"] = bool(value)
                    # Остальные capabilities: сохраняем с instance key
                    else:
                        key = instance if instance else simple_name
                        state[key] = value
        
        return capabilities, state

    @staticmethod
    def _extract_properties(yandex_properties: list) -> tuple[list, dict]:
        """Извлекает properties и их ЗНАЧЕНИЯ (state.value).
        
        Returns:
            Tuple[properties_list, state_dict]
            - properties_list: список информации о properties
            - state_dict: словарь с extracted state values
        """
        properties = []
        state = {}
        
        for prop in yandex_properties:
            prop_type = prop.get("type", "")
            if not prop_type:
                continue
            
            # Сохраняем информацию о property
            prop_info = {
                "type": prop_type,
                "parameters": prop.get("parameters", {}),
                "retrievable": prop.get("retrievable", False),
                "reportable": prop.get("reportable", False),
            }
            properties.append(prop_info)
            
            # КРИТИЧНО: Извлекаем ЗНАЧЕНИЕ из property (state.value)
            prop_state = prop.get("state")
            if isinstance(prop_state, dict):
                value = prop_state.get("value")
                if value is not None:
                    # Используем instance как key (или последний part типа)
                    parameters = prop.get("parameters", {})
                    instance = parameters.get("instance") if isinstance(parameters, dict) else None
                    
                    if instance:
                        state[instance] = value
                    else:
                        # Fallback, если нет instance
                        parts = prop_type.split(".")
                        key = parts[-1] if parts else "unknown"
                        state[key] = value
        
        return properties, state

    @staticmethod
    def _extract_state(yandex_states: list, capabilities: list = None) -> Dict[str, Any]:
        """BACK-COMPAT: Извлекает состояние из списка states (старый формат).
        
        Используется в yandex_quasar_ws.py и operations.py для обратной совместимости.
        
        Args:
            yandex_states: Список объектов со структурой {"type": "...", "state": {...}}
            capabilities: Игнорируется, нужен только для сигнатуры
        
        Returns:
            Dict со значениями состояния
        """
        state = {}
        
        if not isinstance(yandex_states, list):
            return state
        
        for state_item in yandex_states:
            if not isinstance(state_item, dict):
                continue
            
            cap_type = state_item.get("type", "")
            if not cap_type:
                continue
            
            # Извлекаем значение
            state_value = state_item.get("state", {})
            if not isinstance(state_value, dict):
                continue
            
            value = state_value.get("value")
            if value is None:
                continue
            
            # Нормализуем по типу capability: on_off / range / mode / sensor
            parts = cap_type.split(".")
            simple_name = parts[-1] if parts else ""
            instance = state_value.get("instance")

            # on_off → единый логический ключ "on"
            if cap_type.endswith("on_off") or simple_name == "on_off":
                if isinstance(value, bool):
                    state["on"] = value
                elif isinstance(value, str):
                    v = value.strip().lower()
                    state["on"] = v in ("on", "true", "1", "yes")
                elif isinstance(value, (int, float)):
                    state["on"] = bool(value)
                continue

            # range/mode → всегда state[instance] = value (instance обязателен)
            if cap_type.endswith("range") or simple_name == "range" or \
               cap_type.endswith("mode") or simple_name == "mode":
                key = instance or simple_name
                if key:
                    state[key] = value
                continue

            # sensor / прочие capabilities → тоже state[instance] = value, instance приоритетен
            if instance:
                state[instance] = value
            elif simple_name:
                state[simple_name] = value
        
        return state

    @staticmethod
    def convert_params_to_actions(params: Dict[str, Any]) -> list[Dict[str, Any]]:
        actions = []
        if "on" in params:
            actions.append({
                "type": "devices.capabilities.on_off",
                "state": {"instance": "on", "value": params["on"]}
            })
        if "brightness" in params:
            actions.append({
                "type": "devices.capabilities.range",
                "state": {"instance": "brightness", "value": params["brightness"]}
            })
        return actions

    @staticmethod
    def convert_params_to_quasar_states(params: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Формат для Quasar API: state только с value (без instance), часть клиентов так ожидает."""
        states = []
        if "on" in params:
            states.append({
                "type": "devices.capabilities.on_off",
                "state": {"value": bool(params["on"])}
            })
        if "brightness" in params:
            states.append({
                "type": "devices.capabilities.range",
                "state": {"instance": "brightness", "value": params["brightness"]}
            })
        return states
