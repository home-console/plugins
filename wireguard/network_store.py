"""Хранилище сетей и пиров через SDK storage.

Схема: namespace='wireguard', key='networks'
Значение: dict[network_name, NetworkDict]

NetworkDict:
  name, interface, port, subnet, server_ip,
  server_private_key, server_public_key,
  dns, allowed_ips, endpoint, keepalive,
  next_ip_suffix, peers: dict[client_id, PeerDict]

PeerDict:
  ip, private_key, public_key, psk, created_at
"""
from __future__ import annotations

import ipaddress
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

NS = "wireguard"
KEY = "networks"

# Диапазон портов для автоматического выделения.
# Можно переопределить через env WG_PORT_RANGE_START / WG_PORT_RANGE_END.
DEFAULT_PORT_RANGE_START = 51820
DEFAULT_PORT_RANGE_END   = 51879  # 60 сетей максимум


class NetworkStore:
    def __init__(self, plugin: Any, port_start: int = DEFAULT_PORT_RANGE_START, port_end: int = DEFAULT_PORT_RANGE_END) -> None:
        self._p = plugin
        self._port_start = port_start
        self._port_end   = port_end

    # ------------------------------------------------------------------
    # Low-level persistence

    async def _load(self) -> dict[str, Any]:
        data = await self._p.storage_get(NS, KEY)
        return data if isinstance(data, dict) else {}

    async def _save(self, networks: dict[str, Any]) -> None:
        await self._p.storage_set(NS, KEY, networks)

    # ------------------------------------------------------------------
    # Network CRUD

    async def create(
        self,
        name: str,
        subnet: str,
        port: int,
        interface: str,
        server_priv: str,
        server_pub: str,
        *,
        dns: str = "1.1.1.1",
        allowed_ips: str = "0.0.0.0/0",
        endpoint: str | None = None,
        keepalive: int = 25,
    ) -> dict[str, Any]:
        net = ipaddress.ip_network(subnet, strict=False)
        server_ip = str(net.network_address + 1)
        networks = await self._load()
        networks[name] = {
            "name": name,
            "interface": interface,
            "port": port,
            "subnet": subnet,
            "server_ip": server_ip,
            "server_private_key": server_priv,
            "server_public_key": server_pub,
            "dns": dns,
            "allowed_ips": allowed_ips,
            "endpoint": endpoint,
            "keepalive": keepalive,
            "next_ip_suffix": 2,
            "peers": {},
        }
        await self._save(networks)
        return networks[name]

    async def get(self, name: str) -> dict[str, Any] | None:
        networks = await self._load()
        return networks.get(name)

    async def all(self) -> dict[str, Any]:
        return await self._load()

    async def delete(self, name: str) -> bool:
        networks = await self._load()
        if name not in networks:
            return False
        del networks[name]
        await self._save(networks)
        return True

    async def update(self, name: str, network: dict[str, Any]) -> None:
        networks = await self._load()
        networks[name] = network
        await self._save(networks)

    # ------------------------------------------------------------------
    # Peer management

    async def add_peer(
        self,
        network_name: str,
        client_id: str,
        client_priv: str,
        client_pub: str,
        psk: str,
    ) -> dict[str, Any] | None:
        networks = await self._load()
        net = networks.get(network_name)
        if net is None:
            return None

        # Если пир уже есть — вернуть существующий
        if client_id in net["peers"]:
            return net["peers"][client_id]

        # Выделить IP
        base = ipaddress.ip_network(net["subnet"], strict=False)
        suffix = net["next_ip_suffix"]
        if suffix > 254:
            logger.error("wireguard: IP pool exhausted in network '%s'", network_name)
            return None

        ip = str(base.network_address + suffix)
        peer: dict[str, Any] = {
            "ip": ip,
            "private_key": client_priv,
            "public_key": client_pub,
            "psk": psk,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        net["peers"][client_id] = peer
        net["next_ip_suffix"] = suffix + 1
        await self._save(networks)
        return peer

    async def get_peer(self, network_name: str, client_id: str) -> dict[str, Any] | None:
        net = await self.get(network_name)
        if net is None:
            return None
        return net["peers"].get(client_id)

    async def remove_peer(self, network_name: str, client_id: str) -> bool:
        networks = await self._load()
        net = networks.get(network_name)
        if net is None or client_id not in net["peers"]:
            return False
        del net["peers"][client_id]
        await self._save(networks)
        return True

    async def list_peers(self, network_name: str) -> list[dict[str, Any]]:
        net = await self.get(network_name)
        if net is None:
            return []
        return [
            {"client_id": cid, "ip": p["ip"], "public_key": p["public_key"]}
            for cid, p in net["peers"].items()
        ]

    # ------------------------------------------------------------------
    # Interface name allocation

    async def next_interface(self) -> str:
        networks = await self._load()
        used = {n["interface"] for n in networks.values()}
        for i in range(0, 64):
            iface = f"wg{i}"
            if iface not in used:
                return iface
        return "wg64"

    # Port pool

    async def used_ports(self) -> set[int]:
        networks = await self._load()
        return {n["port"] for n in networks.values()}

    async def next_port(self) -> int | None:
        """Выдать следующий свободный порт из пула. None если пул исчерпан."""
        used = await self.used_ports()
        for port in range(self._port_start, self._port_end + 1):
            if port not in used:
                return port
        return None

    async def port_pool_info(self) -> dict[str, Any]:
        """Информация о пуле портов: диапазон, занятые, свободные."""
        used = await self.used_ports()
        total    = self._port_end - self._port_start + 1
        free     = [p for p in range(self._port_start, self._port_end + 1) if p not in used]
        return {
            "range_start":  self._port_start,
            "range_end":    self._port_end,
            "total":        total,
            "used":         len(used),
            "free":         len(free),
            "free_ports":   free,
            "used_ports":   sorted(used),
            "capacity_pct": round(len(used) / total * 100, 1),
        }
