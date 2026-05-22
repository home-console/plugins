# Network Scanner Plugin

Плагин для сканирования локальной сети и автоматического обнаружения устройств в сети.

## Функциональность

- **Быстрое обнаружение хостов** через ICMP ping
- **Разрешение hostname** через обратное DNS
- **Детальное сканирование** (опционально) через nmap:
  - Определение открытых портов
  - Идентификация сервисов
  - Определение операционной системы (требует root)
- **Периодическое автоматическое сканирование** через asyncio scheduler
- **REST API** через service registry (операции)
- **Интеграция с devices модулем** через события

## Services

Плагин регистрирует следующие сервисы в `service_registry`:

### `network_scanner.scan`
Сканирование сети (требует прав администратора).

**Параметры:**
```python
{
    "network": "192.168.1.0/24",  # опционально - CIDR адрес
    "enable_nmap": false,          # опционально - использовать nmap
    "enable_os_detection": false,  # опционально - определять ОС (требует root)
    "timeout": 2                   # опционально - таймаут для ping
}
```

**Результат:**
```python
{
    "status": "success",
    "hosts_found": 5,
    "hosts": [
        {
            "ip_address": "192.168.1.100",
            "hostname": "router.local",
            "mac_address": null,
            "os_type": null,
            "open_ports": [],
            "services": [],
            "is_online": true,
            "last_seen": "2026-02-25T10:30:45.123456"
        }
    ]
}
```

### `network_scanner.get_local_networks`
Получить список локальных сетей (доступно всем).

**Результат:**
```python
["192.168.1.0/24", "10.0.0.0/8"]
```

### `network_scanner.get_discovered_hosts`
Получить список обнаруженных хостов (доступно всем).

**Результат:**
```python
[
    {
        "ip_address": "192.168.1.100",
        "hostname": "router.local",
        "mac_address": null,
        ...
    }
]
```

### `network_scanner.enable_auto_scan`
Включить периодическое автоматическое сканирование (требует прав администратора).

**Параметры:**
```python
{
    "interval_seconds": 300,  # интервал между сканированиями
    "networks": None          # список сетей (если None - сканируются все локальные)
}
```

### `network_scanner.disable_auto_scan`
Отключить периодическое автоматическое сканирование (требует прав администратора).

### `network_scanner.get_auto_scan_status`
Получить статус периодического сканирования (доступно всем).

## Events

Плагин публикует следующие события при обнаружении устройств:

### `external.device_discovered`
Событие при обнаружении нового устройства.

**Payload:**
```json
{
  "ip_address": "192.168.1.100",
  "hostname": "router.local",
  "mac_address": null,
  "os_type": null,
  "open_ports": [],
  "services": []
}
```

Это событие может быть обработано модулем `devices` для автоматического добавления устройства в инвентарь.

## Requirements

```
nmap>=0.0.1
python-nmap>=0.0.1
scapy>=2.5.0
netifaces>=0.11.0
```

## Installation

Плагин автоматически загружается при старте runtime если находится в папке `plugins/network_scanner/`.

## Примеры использования

### Сканирование конкретной сети
```bash
curl -X POST "http://localhost:8000/plugins/network_scanner/scan?network=192.168.1.0/24&timeout=3"
```

### Сканирование всех локальных сетей
```bash
curl -X POST "http://localhost:8000/plugins/network_scanner/scan"
```

### Сканирование с nmap
```bash
curl -X POST "http://localhost:8000/plugins/network_scanner/scan?enable_nmap=true&timeout=5"
```

### Получить список хостов
```bash
curl "http://localhost:8000/plugins/network_scanner/hosts"
```

## Архитектура

```
NetworkScannerPlugin (BasePlugin)
├── NetworkScanner (логика сканирования)
│   ├── get_local_networks() — получить локальные сети
│   ├── ping_scan() — быстрое сканирование через ping
│   ├── resolve_hostname() — обратное DNS
│   └── nmap_scan() — детальное сканирование через nmap
├── REST Handlers (handlers.py)
│   ├── GET /networks
│   ├── POST /scan
│   ├── GET /hosts
│   ├── GET /hosts/{ip}
│   └── DELETE /hosts
└── Services (service_registry)
    ├── network_scanner.scan
    ├── network_scanner.get_local_networks
    └── network_scanner.get_discovered_hosts
```

## Планы развития

- [ ] SSH интеграция для удалённого сканирования сетей
- [ ] Persistent хранилище обнаруженных хостов (в storage)
- [ ] Периодическое автоматическое сканирование
- [ ] Детектирование типов устройств (принтеры, камеры, NAS и т.д.)
- [ ] WebSocket для реал-тайм обновлений сканирования
- [ ] Интеграция с WireGuard для настройки туннелей к обнаруженным хостам
