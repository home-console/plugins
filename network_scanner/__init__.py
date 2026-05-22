"""
Плагин Network Scanner — сканирование локальной сети и обнаружение устройств.

Экспортирует NetworkScannerPlugin для использования в runtime.
"""

from .plugin import NetworkScannerPlugin

__all__ = ["NetworkScannerPlugin"]
