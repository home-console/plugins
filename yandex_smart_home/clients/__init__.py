"""Clients package for Yandex Smart Home plugin (HTTP clients, oauth facade).

Note:
- The oauth facade (get_access_token, get_status, get_cookies) is implemented
  in the top-level module `plugins.yandex_smart_home.oauth_provider`.
- This package provides a thin client wrapper used by other modules.
"""

from .api_client import YandexAPIClient

__all__ = ["YandexAPIClient"]
