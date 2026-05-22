"""
Network Scanner — логика сканирования сети и обнаружения хостов.

Поддерживает:
- ICMP ping (быстрое обнаружение)
- ARP scan (для локальных сетей)
- nmap (детальное сканирование портов)
- Получение информации об ОС и сервисах
"""

from __future__ import annotations

import asyncio
import subprocess
import json
import re
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import ipaddress
import socket

try:
    import netifaces  # type: ignore
except ImportError:  # pragma: no cover
    netifaces = None


@dataclass
class NetworkHost:
    """Информация об обнаруженном хосте."""
    ip_address: str
    mac_address: Optional[str] = None
    hostname: Optional[str] = None
    os_type: Optional[str] = None
    open_ports: List[int] = None
    services: List[Dict[str, str]] = None
    is_online: bool = True
    last_seen: Optional[str] = None

    def __post_init__(self):
        if self.open_ports is None:
            self.open_ports = []
        if self.services is None:
            self.services = []
        if self.last_seen is None:
            self.last_seen = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Преобразование в словарь."""
        return asdict(self)


class NetworkScanner:
    """Основной класс для сканирования сети."""

    def __init__(self, logger=None):
        """
        Инициализация сканера.
        
        Args:
            logger: опциональный логгер
        """
        self.logger = logger
        self.discovered_hosts: Dict[str, NetworkHost] = {}

    def _log(self, level: str, msg: str) -> None:
        """Логирование."""
        if self.logger:
            getattr(self.logger, level, print)(f"[NetworkScanner] {msg}")
        else:
            print(f"[{level.upper()}] {msg}")

    async def get_local_networks(self) -> List[str]:
        """
        Получить список локальных сетей (CIDR нотация).
        
        Returns:
            Список CIDR адресов типа ['192.168.1.0/24', '10.0.0.0/8']
        """
        if netifaces is None:
            self._log(
                "warning",
                "netifaces is not installed; get_local_networks() returns [].",
            )
            return []

        networks = []
        try:
            for interface in netifaces.interfaces():
                try:
                    if_addrs = netifaces.ifaddresses(interface)
                    if netifaces.AF_INET in if_addrs:
                        for addr_info in if_addrs[netifaces.AF_INET]:
                            ip = addr_info.get('addr')
                            netmask = addr_info.get('netmask')
                            if ip and netmask and not ip.startswith('127'):
                                # Конвертируем в CIDR
                                network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
                                networks.append(str(network))
                except Exception as e:
                    self._log('debug', f"Ошибка при обработке интерфейса {interface}: {e}")
        except Exception as e:
            self._log('error', f"Ошибка при получении сетевых интерфейсов: {e}")
        
        return networks

    async def ping_scan(self, network: str, timeout: int = 2) -> List[str]:
        """
        Быстрое сканирование сети через ICMP ping.
        
        Args:
            network: CIDR адрес (например, '192.168.1.0/24')
            timeout: таймаут для каждого пинга в секундах
            
        Returns:
            Список доступных IP адресов
        """
        self._log('info', f"Запуск ping сканирования: {network}")
        
        try:
            network_obj = ipaddress.ip_network(network, strict=False)
            alive_hosts = []
            
            # Сканируем все адреса в сети (кроме broadcast и network)
            for ip in list(network_obj.hosts())[:min(254, network_obj.num_addresses - 2)]:
                ip_str = str(ip)
                try:
                    result = subprocess.run(
                        ['ping', '-c', '1', '-W', str(timeout), ip_str],
                        capture_output=True,
                        timeout=timeout + 1
                    )
                    if result.returncode == 0:
                        alive_hosts.append(ip_str)
                        self._log('debug', f"Host {ip_str} доступен")
                except Exception as e:
                    self._log('debug', f"Ошибка при пинге {ip_str}: {e}")
            
            self._log('info', f"Ping сканирование завершено. Найдено {len(alive_hosts)} хостов")
            return alive_hosts
            
        except Exception as e:
            self._log('error', f"Ошибка при ping сканировании {network}: {e}")
            return []

    async def resolve_hostname(self, ip: str) -> Optional[str]:
        """
        Разрешить IP адрес в hostname (обратное DNS).
        
        Args:
            ip: IP адрес
            
        Returns:
            Hostname или None
        """
        try:
            hostname, _, _ = socket.gethostbyaddr(ip)
            return hostname
        except (socket.herror, socket.timeout):
            return None
        except Exception as e:
            self._log('debug', f"Ошибка при разрешении {ip}: {e}")
            return None

    async def nmap_scan(self, targets: List[str], enable_os_detection: bool = False) -> Dict[str, NetworkHost]:
        """
        Детальное сканирование через nmap.
        
        Args:
            targets: список IP адресов или CIDR сетей
            enable_os_detection: включить определение ОС (требует root)
            
        Returns:
            Словарь {ip: NetworkHost}
        """
        self._log('info', f"Запуск nmap сканирования для {len(targets)} целей")
        
        results = {}
        
        try:
            # Используем nmap для сканирования портов и определения сервисов
            nmap_args = [
                'nmap',
                '-sV',  # Определение версий сервисов
                '--open',  # Показать только открытые порты
                '-oG', '-',  # Grepable output в stdout
            ]
            
            if enable_os_detection:
                nmap_args.append('-O')  # Определение ОС
            
            nmap_args.extend(targets)
            
            # Запускаем nmap в фоновом потоке
            result = await asyncio.to_thread(
                subprocess.run,
                nmap_args,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode == 0:
                # Парсим результаты
                for line in result.stdout.split('\n'):
                    if line.startswith('Host:'):
                        # Пример: Host: 192.168.1.100	Status: Up	Ignored State: closed (999)
                        parts = line.split()
                        if len(parts) >= 2:
                            ip = parts[1]
                            host = NetworkHost(ip_address=ip, is_online=True)
                            
                            # Пытаемся разрешить hostname
                            hostname = await self.resolve_hostname(ip)
                            if hostname:
                                host.hostname = hostname
                            
                            results[ip] = host
            else:
                self._log('error', f"nmap ошибка: {result.stderr}")
                
        except Exception as e:
            self._log('error', f"Ошибка при nmap сканировании: {e}")
        
        return results

    async def scan_network(
        self,
        network: Optional[str] = None,
        enable_nmap: bool = False,
        enable_os_detection: bool = False,
        timeout: int = 2
    ) -> List[NetworkHost]:
        """
        Полное сканирование сети.
        
        Args:
            network: CIDR адрес (если None, сканируем все локальные сети)
            enable_nmap: использовать nmap для детального сканирования
            enable_os_detection: определять ОС (требует root)
            timeout: таймаут для пинга
            
        Returns:
            Список обнаруженных хостов
        """
        # Определяем сети для сканирования
        networks_to_scan = []
        if network:
            networks_to_scan = [network]
        else:
            networks_to_scan = await self.get_local_networks()
        
        if not networks_to_scan:
            self._log('warning', "Не удалось определить локальные сети")
            return []
        
        self._log('info', f"Сканирование сетей: {networks_to_scan}")
        
        all_hosts = []
        
        # Ping сканирование
        for network in networks_to_scan:
            alive_ips = await self.ping_scan(network, timeout=timeout)
            
            for ip in alive_ips:
                host = NetworkHost(ip_address=ip, is_online=True)
                
                # Разрешаем hostname
                hostname = await self.resolve_hostname(ip)
                if hostname:
                    host.hostname = hostname
                
                self.discovered_hosts[ip] = host
                all_hosts.append(host)
        
        # Детальное нmap сканирование если включено
        if enable_nmap and all_hosts:
            target_ips = [h.ip_address for h in all_hosts]
            nmap_results = await self.nmap_scan(target_ips, enable_os_detection=enable_os_detection)
            
            # Объединяем результаты
            for ip, host in nmap_results.items():
                if ip in self.discovered_hosts:
                    # Обновляем существующий хост
                    existing = self.discovered_hosts[ip]
                    existing.open_ports = host.open_ports
                    existing.services = host.services
                    existing.os_type = host.os_type
                else:
                    self.discovered_hosts[ip] = host
                    all_hosts.append(host)
        
        self._log('info', f"Сканирование завершено. Найдено {len(all_hosts)} хостов")
        return all_hosts

    def get_discovered_hosts(self) -> List[NetworkHost]:
        """Получить список всех обнаруженных хостов."""
        return list(self.discovered_hosts.values())

    def clear_discovered_hosts(self) -> None:
        """Очистить список обнаруженных хостов."""
        self.discovered_hosts.clear()
