"""
WireGuard плагин для HomeConsole Core.

Управляет несколькими WireGuard сетями. Каждая сеть — отдельный wg-интерфейс
с собственной подсетью и ключами.

Сервисы:
  wireguard.create_network(name, subnet, port, **opts)
  wireguard.delete_network(name)
  wireguard.list_networks()
  wireguard.network_status(name)
  wireguard.get_config(client_id, network="default")
  wireguard.add_peer(client_id, network="default")
  wireguard.remove_peer(client_id, network="default")
  wireguard.list_peers(network="default")
  wireguard.peer_stats(network="default")
  wireguard.mesh_health(network="default")

События:
  wireguard.network_created   {name, interface, subnet, port}
  wireguard.network_deleted   {name}
  wireguard.peer_added        {client_id, network, ip}
  wireguard.peer_removed      {client_id, network}
  wireguard.tunnel_up         {client_id, network, ip}
  wireguard.tunnel_down       {client_id, network, last_handshake_ts}

Конфиг через env (опционально):
  WG_DEFAULT_SUBNET    — подсеть сети default (по умолчанию 10.88.0.0/24)
  WG_DEFAULT_PORT      — порт сети default (по умолчанию 51820)
  WG_DEFAULT_ENDPOINT  — публичный endpoint для клиентских конфигов
  WG_DEFAULT_DNS       — DNS для клиентов (по умолчанию 1.1.1.1)
  WG_DEFAULT_ALLOWED   — AllowedIPs для клиентов (по умолчанию 0.0.0.0/0)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sdk.plugin_ext import BasePlugin, PluginMetadata

from .network_store import NetworkStore
from . import wg_manager as wg

logger = logging.getLogger(__name__)

# Туннель считается мёртвым если последний handshake > 3 минут назад
_HANDSHAKE_DEAD_SEC = 180
_POLL_INTERVAL_SEC  = 60


class WireGuardPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="wireguard",
        version="0.1.0",
        description="WireGuard VPN: множественные сети, peer-менеджмент, mesh-мониторинг",
    )

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._store: NetworkStore | None = None
        self._poll_task: asyncio.Task | None = None
        # Отслеживаем состояние туннелей между polling-тиками {network: {client_id: bool}}
        self._tunnel_state: dict[str, dict[str, bool]] = {}

    # ------------------------------------------------------------------
    # Lifecycle

    async def on_load(self) -> None:
        port_start = self.get_env_config_int("WG_PORT_RANGE_START", 51820)
        port_end   = self.get_env_config_int("WG_PORT_RANGE_END",   51879)
        self._store = NetworkStore(self, port_start=port_start, port_end=port_end)
        await self._register_services()
        await self._register_http()
        await self.subscribe_event("client.connected", self._on_client_connected)

    async def on_start(self) -> None:
        if not wg.is_available():
            logger.warning("wireguard: wg/wg-quick not found — плагин запущен, но интерфейсы не поднимаются")
            return

        # Создать сеть default если нет ни одной
        await self._ensure_default_network()

        # Поднять все сохранённые сети
        networks = await self._store.all()
        for name, net in networks.items():
            await self._start_network(name, net)

        # Фоновый мониторинг туннелей
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def on_stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Default network bootstrap

    async def _ensure_default_network(self) -> None:
        nets = await self._store.all()
        if nets:
            return
        subnet   = self.get_env_config("WG_DEFAULT_SUBNET",   "10.88.0.0/24")
        port     = self.get_env_config_int("WG_DEFAULT_PORT", 51820)
        dns      = self.get_env_config("WG_DEFAULT_DNS",      "1.1.1.1")
        allowed  = self.get_env_config("WG_DEFAULT_ALLOWED",  "0.0.0.0/0")
        endpoint = self.get_env_config("WG_DEFAULT_ENDPOINT", "") or None
        iface    = await self._store.next_interface()
        priv = wg.genkey()
        pub  = wg.pubkey(priv)
        await self._store.create(
            "default", subnet, port, iface, priv, pub,
            dns=dns, allowed_ips=allowed, endpoint=endpoint,
        )
        logger.info("wireguard: создана сеть 'default' (%s, %s:%d)", iface, subnet, port)

    # ------------------------------------------------------------------
    # Network start/stop

    async def _start_network(self, name: str, net: dict[str, Any]) -> bool:
        if wg.is_up(net["interface"]):
            logger.debug("wireguard: %s уже поднят", net["interface"])
            return True
        try:
            conf_path = wg.write_conf(net)
            ok = wg.up(conf_path)
            if ok:
                logger.info("wireguard: сеть '%s' (%s) поднята", name, net["interface"])
            else:
                logger.error("wireguard: не удалось поднять '%s'", name)
            return ok
        except Exception as e:
            logger.error("wireguard: ошибка запуска '%s': %s", name, e)
            return False

    async def _stop_network(self, name: str, net: dict[str, Any]) -> None:
        try:
            wg.down(net["interface"])
            logger.info("wireguard: сеть '%s' остановлена", name)
        except Exception as e:
            logger.warning("wireguard: ошибка остановки '%s': %s", name, e)

    # ------------------------------------------------------------------
    # Background polling

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_POLL_INTERVAL_SEC)
                await self._check_all_tunnels()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("wireguard: ошибка polling: %s", e)

    async def _check_all_tunnels(self) -> None:
        """Polling: проверяем туннели локально (wg show) и дистанционно (mesh_snapshot)."""
        networks = await self._store.all()
        now = int(time.time())

        for name, net in networks.items():
            iface = net["interface"]
            prev  = self._tunnel_state.setdefault(name, {})

            # --- Локальный чек (этот узел) ---
            if wg.is_up(iface):
                stats     = wg.show_stats(iface)
                pub_to_id = {p["public_key"]: cid for cid, p in net["peers"].items()}

                for pub, stat in stats.items():
                    client_id = pub_to_id.get(pub)
                    if not client_id:
                        continue
                    hs    = stat.get("last_handshake_ts", 0)
                    alive = hs > 0 and (now - hs) < _HANDSHAKE_DEAD_SEC
                    was_alive = prev.get(client_id, True)

                    if alive and not was_alive:
                        prev[client_id] = True
                        await self.publish_event("wireguard.tunnel_up", {
                            "client_id": client_id, "network": name,
                            "ip": net["peers"].get(client_id, {}).get("ip"),
                            "perspective": "local",
                        })
                    elif not alive and was_alive:
                        prev[client_id] = False
                        await self.publish_event("wireguard.tunnel_down", {
                            "client_id": client_id, "network": name,
                            "last_handshake_ts": hs,
                            "perspective": "local",
                        })

            # --- Дистанционный чек через агентов ---
            try:
                snapshot = await self.call_service("wireguard.mesh_snapshot", network=name)
                if not snapshot or not snapshot.get("ok"):
                    continue
                for tunnel in snapshot.get("tunnels", []):
                    if not tunnel["alive"]:
                        key = f"{tunnel['from']}→{tunnel['to']}"
                        # Публикуем только если не было уже от локального чека
                        await self.publish_event("wireguard.tunnel_down", {
                            "client_id":    tunnel["to"],
                            "from_agent":   tunnel["from"],
                            "network":      name,
                            "perspective":  "remote",
                            "last_handshake_ago": tunnel.get("last_handshake_ago"),
                        })
            except Exception as e:
                logger.debug("wireguard mesh polling error: %s", e)

    # ------------------------------------------------------------------
    # Event handlers

    async def _on_client_connected(self, payload: dict[str, Any]) -> None:
        """При подключении агента — автоматически добавить пир если указана сеть."""
        client_id = payload.get("client_id") or payload.get("id", "")
        network   = payload.get("wg_network", "")
        if not client_id or not network:
            return
        nets = await self._store.all()
        if network not in nets:
            return
        try:
            await self._do_add_peer(client_id, network)
            logger.info("wireguard: auto-added peer %s → network '%s'", client_id, network)
        except Exception as e:
            logger.warning("wireguard: auto-add peer failed for %s: %s", client_id, e)

    # ------------------------------------------------------------------
    # Service implementations

    async def _do_add_peer(self, client_id: str, network: str) -> dict[str, Any] | None:
        net = await self._store.get(network)
        if net is None:
            return None
        # Если пир уже существует — вернуть конфиг
        existing = await self._store.get_peer(network, client_id)
        if existing:
            return existing
        # Генерируем ключи
        priv = wg.genkey()
        pub  = wg.pubkey(priv)
        psk  = wg.genpsk()
        peer = await self._store.add_peer(network, client_id, priv, pub, psk)
        if peer is None:
            return None
        # Добавляем в live-интерфейс
        if wg.is_up(net["interface"]):
            wg.set_peer(net["interface"], pub, psk, f"{peer['ip']}/32")
        await self.publish_event("wireguard.peer_added", {
            "client_id": client_id, "network": network, "ip": peer["ip"],
        })
        return peer

    # ------------------------------------------------------------------
    # Service registration

    async def _register_services(self) -> None:
        store = self._store

        # ---- Network management ----

        async def port_pool(**_: Any) -> dict[str, Any]:
            return await store.port_pool_info()

        async def create_network(
            name: str,
            subnet: str = "10.0.0.0/24",
            port: int = 0,
            interface: str | None = None,
            dns: str = "1.1.1.1",
            allowed_ips: str = "0.0.0.0/0",
            endpoint: str | None = None,
            keepalive: int = 25,
            **_: Any,
        ) -> dict[str, Any]:
            nets = await store.all()
            if name in nets:
                return {"error": f"Сеть '{name}' уже существует", "ok": False}

            # Авто-порт если не указан
            if not port:
                port = await store.next_port()
                if port is None:
                    pool = await store.port_pool_info()
                    return {
                        "error": f"Пул портов исчерпан ({pool['range_start']}–{pool['range_end']}). "
                                 "Увеличь WG_PORT_RANGE_END или удали неиспользуемые сети.",
                        "ok": False,
                    }

            # Проверяем что порт не занят
            if port in await store.used_ports():
                return {"error": f"Порт {port} уже используется другой сетью.", "ok": False}

            iface = interface or await store.next_interface()
            priv = wg.genkey()
            pub  = wg.pubkey(priv)
            net = await store.create(
                name, subnet, port, iface, priv, pub,
                dns=dns, allowed_ips=allowed_ips,
                endpoint=endpoint, keepalive=keepalive,
            )
            if wg.is_available():
                await self._start_network(name, net)
            await self.publish_event("wireguard.network_created", {
                "name": name, "interface": iface, "subnet": subnet, "port": port,
            })
            return {"ok": True, "network": name, "interface": iface, "subnet": subnet, "port": port}

        async def delete_network(name: str, **_: Any) -> dict[str, Any]:
            net = await store.get(name)
            if net is None:
                return {"error": f"Сеть '{name}' не найдена", "ok": False}
            if wg.is_available() and wg.is_up(net["interface"]):
                await self._stop_network(name, net)
            await store.delete(name)
            await self.publish_event("wireguard.network_deleted", {"name": name})
            return {"ok": True, "name": name}

        async def list_networks(**_: Any) -> dict[str, Any]:
            nets = await store.all()
            result = []
            for name, net in nets.items():
                up = wg.is_up(net["interface"]) if wg.is_available() else None
                result.append({
                    "name": name,
                    "interface": net["interface"],
                    "subnet": net["subnet"],
                    "port": net["port"],
                    "peers": len(net.get("peers", {})),
                    "up": up,
                })
            return {"networks": result, "total": len(result)}

        async def network_status(name: str = "default", **_: Any) -> dict[str, Any]:
            net = await store.get(name)
            if net is None:
                return {"error": f"Сеть '{name}' не найдена", "ok": False}
            stats: dict[str, Any] = {}
            if wg.is_available():
                stats = wg.show_stats(net["interface"])
            return {
                "ok": True,
                "name": name,
                "interface": net["interface"],
                "subnet": net["subnet"],
                "port": net["port"],
                "up": wg.is_up(net["interface"]) if wg.is_available() else None,
                "peers_total": len(net.get("peers", {})),
                "peers_active": len(stats),
            }

        # ---- Peer management ----

        async def get_config(client_id: str, network: str = "default", **_: Any) -> dict[str, Any]:
            net = await store.get(network)
            if net is None:
                return {"error": f"Сеть '{network}' не найдена", "ok": False}
            peer = await self._do_add_peer(client_id, network)
            if peer is None:
                return {"error": "Не удалось создать конфиг", "ok": False}
            config_text = wg.client_conf(net, peer)
            return {
                "ok": True,
                "type": "wireguard_config",
                "client_id": client_id,
                "network": network,
                "private_key": peer["private_key"],
                "public_key": peer["public_key"],
                "server_public_key": net["server_public_key"],
                "address": peer["ip"],
                "dns": net.get("dns", "1.1.1.1"),
                "endpoint": net.get("endpoint") or f"YOUR_SERVER_IP:{net['port']}",
                "allowed_ips": net.get("allowed_ips", "0.0.0.0/0"),
                "keepalive": net.get("keepalive", 25),
                "config_text": config_text,
            }

        async def add_peer(client_id: str, network: str = "default", **_: Any) -> dict[str, Any]:
            peer = await self._do_add_peer(client_id, network)
            if peer is None:
                return {"error": "Не удалось добавить пир", "ok": False}
            return {"ok": True, "client_id": client_id, "network": network, "ip": peer["ip"]}

        async def remove_peer(client_id: str, network: str = "default", **_: Any) -> dict[str, Any]:
            net = await store.get(network)
            if net is None:
                return {"error": f"Сеть '{network}' не найдена", "ok": False}
            peer = await store.get_peer(network, client_id)
            if peer is None:
                return {"error": f"Пир '{client_id}' не найден", "ok": False}
            if wg.is_available() and wg.is_up(net["interface"]):
                wg.del_peer(net["interface"], peer["public_key"])
            await store.remove_peer(network, client_id)
            await self.publish_event("wireguard.peer_removed", {
                "client_id": client_id, "network": network,
            })
            return {"ok": True, "client_id": client_id, "network": network}

        async def list_peers(network: str = "default", **_: Any) -> dict[str, Any]:
            peers = await store.list_peers(network)
            return {"network": network, "peers": peers, "total": len(peers)}

        # ---- Monitoring ----

        async def peer_stats(network: str = "default", **_: Any) -> dict[str, Any]:
            net = await store.get(network)
            if net is None:
                return {"error": f"Сеть '{network}' не найдена", "ok": False}
            if not wg.is_available():
                return {"ok": False, "error": "wg не установлен"}
            raw = wg.show_stats(net["interface"])
            pub_to_id = {p["public_key"]: cid for cid, p in net["peers"].items()}
            now = int(time.time())
            result = []
            for pub, stat in raw.items():
                client_id = pub_to_id.get(pub, pub[:12] + "…")
                hs = stat.get("last_handshake_ts", 0)
                result.append({
                    "client_id": client_id,
                    "endpoint": stat.get("endpoint"),
                    "last_handshake_ts": hs,
                    "last_handshake_ago": now - hs if hs else None,
                    "alive": hs > 0 and (now - hs) < _HANDSHAKE_DEAD_SEC,
                    "rx_bytes": stat.get("rx_bytes", 0),
                    "tx_bytes": stat.get("tx_bytes", 0),
                })
            return {"ok": True, "network": network, "peers": result}

        async def mesh_health(network: str = "default", **_: Any) -> dict[str, Any]:
            stats_result = await peer_stats(network=network)
            if not stats_result.get("ok"):
                return stats_result
            peers = stats_result["peers"]
            alive  = [p for p in peers if p["alive"]]
            dead   = [p for p in peers if not p["alive"]]
            return {
                "ok": True,
                "network": network,
                "healthy": len(dead) == 0,
                "total": len(peers),
                "alive": len(alive),
                "dead": len(dead),
                "tunnels": peers,
            }

        # ---- Mesh monitoring ----

        async def tunnel_status(
            agent_id: str,
            network: str = "default",
            iface: str | None = None,
            **_: Any,
        ) -> dict[str, Any]:
            """Запросить `wg show dump` на удалённом агенте и вернуть статус его туннелей."""
            net = await store.get(network)
            if net is None:
                return {"error": f"Сеть '{network}' не найдена", "ok": False}

            interface = iface or net["interface"]
            now = int(time.time())

            try:
                result = await self.call_service(
                    "client_manager._impl.execute_command",
                    client_id=agent_id,
                    body={"command": f"wg show {interface} dump", "timeout": 10},
                )
            except Exception as e:
                return {"ok": False, "agent_id": agent_id, "error": str(e)}

            if not result or result.get("error"):
                return {"ok": False, "agent_id": agent_id, "error": result.get("error", "no result") if result else "no response"}

            raw_output = ""
            if isinstance(result.get("result"), str):
                raw_output = result["result"]
            elif isinstance(result.get("result"), dict):
                raw_output = result["result"].get("output", "") or result["result"].get("stdout", "")

            if not raw_output:
                return {"ok": False, "agent_id": agent_id, "error": "пустой вывод wg show"}

            stats = wg.parse_dump_str(raw_output)
            pub_to_id = {p["public_key"]: cid for cid, p in net["peers"].items()}

            tunnels = []
            for pub, stat in stats.items():
                peer_id = pub_to_id.get(pub, f"unknown:{pub[:12]}")
                hs = stat.get("last_handshake_ts", 0)
                tunnels.append({
                    "peer_id": peer_id,
                    "endpoint": stat.get("endpoint"),
                    "last_handshake_ts": hs,
                    "last_handshake_ago": now - hs if hs else None,
                    "alive": hs > 0 and (now - hs) < _HANDSHAKE_DEAD_SEC,
                    "rx_bytes": stat.get("rx_bytes", 0),
                    "tx_bytes": stat.get("tx_bytes", 0),
                })

            return {
                "ok": True,
                "agent_id": agent_id,
                "network": network,
                "interface": interface,
                "tunnels": tunnels,
            }

        async def mesh_snapshot(network: str = "default", **_: Any) -> dict[str, Any]:
            """Снапшот всего mesh: опросить каждый подключённый агент-пир и собрать матрицу туннелей.

            Для треугольной топологии (VDS + 2 локации):
              VDS видит Apt1 и Apt2
              Apt1 видит VDS и Apt2
              Apt2 видит VDS и Apt1
            """
            net = await store.get(network)
            if net is None:
                return {"error": f"Сеть '{network}' не найдена", "ok": False}

            peers_in_net = set(net.get("peers", {}).keys())
            if not peers_in_net:
                return {"ok": True, "network": network, "nodes": [], "perspectives": [], "summary": {}}

            # Список подключённых агентов
            try:
                clients_result = await self.call_service("client_manager.list_clients")
                connected_ids: set[str] = set()
                if isinstance(clients_result, list):
                    connected_ids = {c.get("client_id") or c.get("id", "") for c in clients_result}
                elif isinstance(clients_result, dict):
                    items = clients_result.get("result") or clients_result.get("clients") or []
                    connected_ids = {c.get("client_id") or c.get("id", "") for c in items}
            except Exception:
                connected_ids = set()

            # Узлы mesh
            peer_ips = {cid: net["peers"][cid]["ip"] for cid in peers_in_net}
            nodes = [
                {
                    "id": cid,
                    "ip": peer_ips.get(cid, "?"),
                    "connected": cid in connected_ids,
                }
                for cid in sorted(peers_in_net)
            ]

            # Опрашиваем только подключённых агентов
            active_agents = peers_in_net & connected_ids
            perspectives: list[dict[str, Any]] = []

            for agent_id in sorted(active_agents):
                view = await tunnel_status(agent_id=agent_id, network=network)
                perspectives.append(view)

            # Сводка: для каждой пары (from, to) — самый свежий handshake
            tunnel_matrix: dict[str, dict[str, Any]] = {}
            for view in perspectives:
                if not view.get("ok"):
                    continue
                src = view["agent_id"]
                for t in view.get("tunnels", []):
                    dst = t["peer_id"]
                    key = f"{src}→{dst}"
                    tunnel_matrix[key] = {
                        "from": src, "to": dst,
                        "alive": t["alive"],
                        "last_handshake_ago": t.get("last_handshake_ago"),
                        "rx_bytes": t.get("rx_bytes", 0),
                        "tx_bytes": t.get("tx_bytes", 0),
                        "endpoint": t.get("endpoint"),
                    }

            total_tunnels = len(tunnel_matrix)
            dead_tunnels  = sum(1 for t in tunnel_matrix.values() if not t["alive"])

            return {
                "ok": True,
                "network": network,
                "nodes": nodes,
                "tunnels": list(tunnel_matrix.values()),
                "perspectives": perspectives,
                "summary": {
                    "total_nodes": len(nodes),
                    "connected_nodes": len(active_agents),
                    "total_tunnels": total_tunnels,
                    "alive_tunnels": total_tunnels - dead_tunnels,
                    "dead_tunnels": dead_tunnels,
                    "healthy": dead_tunnels == 0,
                },
            }

        # ---- Register all ----

        await self.register_service("wireguard.tunnel_status",   tunnel_status)
        await self.register_service("wireguard.mesh_snapshot",   mesh_snapshot)
        await self.register_service("wireguard.port_pool",       port_pool)
        await self.register_service("wireguard.create_network",  create_network)
        await self.register_service("wireguard.delete_network",  delete_network)
        await self.register_service("wireguard.list_networks",   list_networks)
        await self.register_service("wireguard.network_status",  network_status)
        await self.register_service("wireguard.get_config",      get_config)
        await self.register_service("wireguard.add_peer",        add_peer)
        await self.register_service("wireguard.remove_peer",     remove_peer)
        await self.register_service("wireguard.list_peers",      list_peers)
        await self.register_service("wireguard.peer_stats",      peer_stats)
        await self.register_service("wireguard.mesh_health",     mesh_health)

    # ------------------------------------------------------------------
    # HTTP endpoints

    async def _register_http(self) -> None:
        try:
            from sdk.http import EndpointAuthConfig, HttpEndpoint
        except ImportError:
            logger.warning("wireguard: sdk.http недоступен, HTTP-эндпоинты не зарегистрированы")
            return

        _read  = EndpointAuthConfig(required_scopes=["admin.read"])
        _write = EndpointAuthConfig(required_scopes=["admin.write"])
        base   = "/api/v1/plugins/wireguard"

        endpoints = [
            HttpEndpoint("GET",    f"{base}/port-pool",                               "wireguard.port_pool",      "Статус пула портов",                       _read),
            HttpEndpoint("GET",    f"{base}/networks",                    "wireguard.list_networks",  "Список WireGuard сетей",                   _read),
            HttpEndpoint("POST",   f"{base}/networks",                    "wireguard.create_network", "Создать WireGuard сеть",                   _write),
            HttpEndpoint("GET",    f"{base}/networks/{{name}}/status",    "wireguard.network_status", "Статус сети",                              _read),
            HttpEndpoint("DELETE", f"{base}/networks/{{name}}",           "wireguard.delete_network", "Удалить сеть",                             _write),
            HttpEndpoint("GET",    f"{base}/networks/{{network}}/peers",  "wireguard.list_peers",     "Список пиров",                             _read),
            HttpEndpoint("POST",   f"{base}/networks/{{network}}/peers",  "wireguard.add_peer",       "Добавить пир",                             _write),
            HttpEndpoint("GET",    f"{base}/networks/{{network}}/stats",  "wireguard.peer_stats",     "Статистика туннелей (handshake, трафик)",  _read),
            HttpEndpoint("GET",    f"{base}/networks/{{network}}/health",    "wireguard.mesh_health",    "Здоровье mesh-сети (локальный wg show)",    _read),
            HttpEndpoint("GET",    f"{base}/networks/{{network}}/snapshot", "wireguard.mesh_snapshot",  "Полный снапшот mesh (опрос всех агентов)",  _read),
            HttpEndpoint("GET",    f"{base}/networks/{{network}}/tunnel/{{agent_id}}", "wireguard.tunnel_status", "Статус туннелей с точки зрения агента", _read),
            HttpEndpoint("GET",    f"{base}/config/{{client_id}}",        "wireguard.get_config",     "Получить .conf для клиента (сеть default)", _write),
            HttpEndpoint("GET",    f"{base}/networks/{{network}}/config/{{client_id}}", "wireguard.get_config", "Получить .conf для клиента", _write),
            HttpEndpoint("DELETE", f"{base}/networks/{{network}}/peers/{{client_id}}", "wireguard.remove_peer", "Удалить пир", _write),
        ]
        for ep in endpoints:
            self.register_http_endpoint(ep)
