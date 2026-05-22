"""
Плагин Network Scanner — сканирование локальной сети и обнаружение устройств.

Функциональность:
- Обнаружение хостов в локальных сетях (ICMP ping, DNS reverse lookup)
- Разрешение hostname через обратное DNS
- Определение открытых портов и сервисов (nmap)
- Периодическое автоматическое сканирование
- Публикация событий об обнаруженных устройствах (для devices модуля)

Services:
- network_scanner.scan
- network_scanner.get_local_networks
- network_scanner.get_discovered_hosts
- network_scanner.enable_auto_scan
- network_scanner.disable_auto_scan
- network_scanner.get_auto_scan_status

Events published:
- external.device_discovered
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Optional

from sdk.plugin_ext import BasePlugin, PluginMetadata
from sdk import ExternalDeviceDiscoveredPayload
from .scanner import NetworkScanner

if TYPE_CHECKING:
    pass


class NetworkScannerPlugin(BasePlugin):
    """
    Плагин для сканирования локальной сети и обнаружения устройств.
    
    Lifecycle:
    - on_load() — регистрация сервисов
    - on_start() — инициализация автосканирования если включено
    - on_stop() — остановка фоновых задач
    - on_unload() — очистка ресурсов
    """

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="network_scanner",
            version="0.1.0",
            description="Сканирование локальной сети и обнаружение устройств",
            author="Home Console",
            capabilities_required=[],
        )

    async def on_load(self) -> None:
        """Загрузка: инициализация сканера и регистрация сервисов."""
        await super().on_load()

        # Инициализируем NetworkScanner
        self.scanner = NetworkScanner(logger=None)
        self._background_tasks: set = set()
        self._scan_task: Optional[asyncio.Task] = None
        
        # Флаги для периодического сканирования
        self._auto_scan_enabled = False
        self._auto_scan_interval = 300
        self._auto_scan_networks: Optional[list] = None

        # Регистрируем сервисы в service_registry
        await self._register_services()
        
        await self._log("info", "Network Scanner плагин загружен")

    async def _register_services(self) -> None:
        """Регистрация сервисов в service_registry."""
        
        async def scan_network_service(
            network: Optional[str] = None,
            enable_nmap: bool = False,
            enable_os_detection: bool = False,
            timeout: int = 2
        ) -> dict:
            """Сканирование сети."""
            hosts = await self.scanner.scan_network(
                network=network,
                enable_nmap=enable_nmap,
                enable_os_detection=enable_os_detection,
                timeout=timeout
            )
            
            # Опубликуем события об обнаруженных устройствах
            for host in hosts:
                device_event: ExternalDeviceDiscoveredPayload = {
                    "ip_address": host.ip_address,
                    "hostname": host.hostname,
                    "mac_address": host.mac_address,
                    "os_type": host.os_type,
                    "open_ports": host.open_ports,
                    "services": host.services,
                }
                try:
                    await self.publish_event(
                        "external.device_discovered",
                        device_event
                    )
                except Exception as e:
                    await self._log("error", f"Ошибка при публикации события: {e}")
            
            return {
                "status": "success",
                "hosts_found": len(hosts),
                "hosts": [h.to_dict() for h in hosts]
            }
        
        async def get_local_networks_service() -> list:
            """Получить список локальных сетей."""
            return await self.scanner.get_local_networks()
        
        async def get_discovered_hosts_service() -> list:
            """Получить список обнаруженных хостов."""
            hosts = self.scanner.get_discovered_hosts()
            return [h.to_dict() for h in hosts]
        
        async def enable_auto_scan_service(interval_seconds: int = 300, networks: Optional[list] = None) -> dict:
            """Включить периодическое автоматическое сканирование."""
            await self._enable_auto_scan(interval_seconds, networks)
            return {
                "status": "enabled",
                "interval_seconds": interval_seconds,
                "networks": networks or "all"
            }
        
        async def disable_auto_scan_service() -> dict:
            """Отключить периодическое сканирование."""
            await self._disable_auto_scan()
            return {"status": "disabled"}
        
        async def get_auto_scan_status_service() -> dict:
            """Получить статус периодического сканирования."""
            return {
                "enabled": self._auto_scan_enabled,
                "interval_seconds": self._auto_scan_interval,
                "networks": self._auto_scan_networks or "all",
                "last_scan": self.scanner.discovered_hosts and max(
                    [h.last_seen for h in self.scanner.get_discovered_hosts()],
                    default=None
                )
            }
        
        # Регистрируем сервисы
        try:
            await self.register_service(
                "network_scanner.scan",
                scan_network_service,
                admin_only=True
            )
            await self.register_service(
                "network_scanner.get_local_networks",
                get_local_networks_service,
                admin_only=False
            )
            await self.register_service(
                "network_scanner.get_discovered_hosts",
                get_discovered_hosts_service,
                admin_only=False
            )
            await self.register_service(
                "network_scanner.enable_auto_scan",
                enable_auto_scan_service,
                admin_only=True
            )
            await self.register_service(
                "network_scanner.disable_auto_scan",
                disable_auto_scan_service,
                admin_only=True
            )
            await self.register_service(
                "network_scanner.get_auto_scan_status",
                get_auto_scan_status_service,
                admin_only=False
            )
            await self._log("info", "Сервисы зарегистрированы")
        except Exception as e:
            await self._log("error", f"Ошибка при регистрации сервисов: {e}")
            raise

    async def on_start(self) -> None:
        """Запуск: инициализация автосканирования если включено в конфиге."""
        await self._log("info", "Network Scanner плагин запущен")
        
        # Проверяем переменные окружения для автосканирования
        auto_scan_enabled = os.getenv("NETWORK_SCANNER_AUTO_SCAN", "false").lower() == "true"
        if auto_scan_enabled:
            interval = int(os.getenv("NETWORK_SCANNER_SCAN_INTERVAL", "300"))
            networks = os.getenv("NETWORK_SCANNER_NETWORKS", None)
            networks_list = networks.split(",") if networks else None
            
            try:
                await self._enable_auto_scan(interval, networks_list)
                await self._log("info", f"Автоматическое сканирование включено (интервал: {interval}с)")
            except Exception as e:
                await self._log("error", f"Ошибка при включении автосканирования: {e}")

    async def on_stop(self) -> None:
        """Остановка: отмена фоновых задач."""
        await self._disable_auto_scan()
        
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
        
        if self._background_tasks:
            try:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass
        
        await self._log("info", "Network Scanner плагин остановлен")

    async def on_unload(self) -> None:
        """Выгрузка: очистка ресурсов."""
        self.scanner.clear_discovered_hosts()
        await self._log("info", "Network Scanner плагин выгружен")

    async def _enable_auto_scan(self, interval_seconds: int = 300, networks: Optional[list] = None) -> None:
        """Включить периодическое сканирование сети."""
        if self._auto_scan_enabled:
            await self._log("warning", "Автосканирование уже включено")
            return
        
        self._auto_scan_enabled = True
        self._auto_scan_interval = interval_seconds
        self._auto_scan_networks = networks
        
        self._scan_task = asyncio.create_task(self._periodic_scan_loop())
        self._background_tasks.add(self._scan_task)
        self._scan_task.add_done_callback(self._background_tasks.discard)
        
        await self._log("info", f"Периодическое сканирование включено (интервал: {interval_seconds}с)")
    
    async def _disable_auto_scan(self) -> None:
        """Отключить периодическое сканирование сети."""
        if not self._auto_scan_enabled:
            return
        
        self._auto_scan_enabled = False
        
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        
        await self._log("info", "Периодическое сканирование отключено")
    
    async def _periodic_scan_loop(self) -> None:
        """Фоновая задача для периодического сканирования сети."""
        try:
            while self._auto_scan_enabled:
                try:
                    await self._log("debug", f"Запуск периодического сканирования (интервал: {self._auto_scan_interval}с)")
                    
                    networks = self._auto_scan_networks
                    if not networks:
                        networks = await self.scanner.get_local_networks()
                    
                    for network in networks:
                        try:
                            hosts = await self.scanner.scan_network(network=network)
                            await self._log("info", f"Периодическое сканирование: найдено {len(hosts)} хостов в {network}")
                        except Exception as e:
                            await self._log("error", f"Ошибка при сканировании {network}: {e}")
                    
                    await asyncio.sleep(self._auto_scan_interval)
                    
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    await self._log("error", f"Ошибка в периодическом сканировании: {e}")
                    await asyncio.sleep(self._auto_scan_interval)
        except asyncio.CancelledError:
            await self._log("info", "Периодическое сканирование отменено")
        except Exception as e:
            await self._log("error", f"Критическая ошибка в периодическом сканировании: {e}")

    async def _log(self, level: str, message: str) -> None:
        """SDK-first логирование через service `logger.log` (best-effort)."""
        try:
            await self.call_service(
                "logger.log",
                level=level,
                message=message,
                plugin=self.metadata.name,
            )
        except Exception:
            # Best-effort fallback: avoid crashing plugin because of logging failures.
            pass
