"""
Плагин OAuth Yandex — self-contained реализация OAuth flow для Яндекса.

Экспортирует OAuthYandexPlugin для использования в runtime.
"""

from .plugin import OAuthYandexPlugin

__all__ = ["OAuthYandexPlugin"]
