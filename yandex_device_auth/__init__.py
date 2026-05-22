"""
Yandex Device/QR Authorization Plugin

Server-side авторизация Яндекса через device/QR-flow без browser OAuth и WebView.
Предназначен для получения session cookies для работы с Quasar WebSocket.
"""
from .plugin import YandexDeviceAuthPlugin

__all__ = ["YandexDeviceAuthPlugin"]