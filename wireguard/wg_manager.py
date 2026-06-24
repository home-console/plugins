"""Низкоуровневые операции с WireGuard через subprocess.

Не зависит от SDK — чистая логика wg/wg-quick.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT = 10


class WgNotAvailable(RuntimeError):
    """wg / wg-quick не установлены."""


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------

def genkey() -> str:
    """Генерировать private key."""
    r = subprocess.run(["wg", "genkey"], capture_output=True, text=True, timeout=_TIMEOUT)
    _check(r, "wg genkey")
    return r.stdout.strip()


def pubkey(private_key: str) -> str:
    """Получить public key из private key."""
    r = subprocess.run(
        ["wg", "pubkey"],
        input=private_key, capture_output=True, text=True, timeout=_TIMEOUT,
    )
    _check(r, "wg pubkey")
    return r.stdout.strip()


def genpsk() -> str:
    """Генерировать preshared key."""
    r = subprocess.run(["wg", "genpsk"], capture_output=True, text=True, timeout=_TIMEOUT)
    _check(r, "wg genpsk")
    return r.stdout.strip()


def is_available() -> bool:
    """Проверить что wg и wg-quick установлены."""
    import shutil
    return shutil.which("wg") is not None and shutil.which("wg-quick") is not None


# ---------------------------------------------------------------------------
# Interface lifecycle
# ---------------------------------------------------------------------------

def is_up(iface: str) -> bool:
    """Проверить что интерфейс поднят."""
    r = subprocess.run(
        ["wg", "show", iface],
        capture_output=True, text=True, timeout=_TIMEOUT,
    )
    return r.returncode == 0


def up(conf_path: str) -> bool:
    """Поднять интерфейс через wg-quick."""
    r = subprocess.run(
        ["wg-quick", "up", conf_path],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        logger.error("wg-quick up failed: %s", r.stderr)
        return False
    return True


def down(iface: str) -> bool:
    """Остановить интерфейс через wg-quick."""
    r = subprocess.run(
        ["wg-quick", "down", iface],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        logger.warning("wg-quick down: %s", r.stderr)
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Dynamic peer management (без рестарта интерфейса)
# ---------------------------------------------------------------------------

def set_peer(iface: str, pubkey_: str, psk: str, allowed_ip: str) -> bool:
    """Добавить/обновить пир в live-интерфейсе."""
    cmd = [
        "wg", "set", iface,
        "peer", pubkey_,
        "preshared-key", "/dev/stdin",
        "allowed-ips", allowed_ip,
    ]
    r = subprocess.run(cmd, input=psk, capture_output=True, text=True, timeout=_TIMEOUT)
    if r.returncode != 0:
        logger.error("wg set peer failed: %s", r.stderr)
        return False
    return True


def del_peer(iface: str, pubkey_: str) -> bool:
    """Удалить пир из live-интерфейса."""
    r = subprocess.run(
        ["wg", "set", iface, "peer", pubkey_, "remove"],
        capture_output=True, text=True, timeout=_TIMEOUT,
    )
    if r.returncode != 0:
        logger.error("wg del peer failed: %s", r.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def parse_dump_str(output: str) -> dict[str, dict[str, Any]]:
    """Парсить вывод `wg show {iface} dump` из строки (для удалённого вывода через agent exec)."""
    stats: dict[str, dict[str, Any]] = {}
    for line in output.strip().splitlines()[1:]:  # пропускаем серверную строку
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        peer_pub, _psk, endpoint, _allowed, handshake_ts, rx, tx, *_ = parts
        try:
            stats[peer_pub] = {
                "endpoint": endpoint if endpoint != "(none)" else None,
                "last_handshake_ts": int(handshake_ts),
                "rx_bytes": int(rx),
                "tx_bytes": int(tx),
            }
        except (ValueError, IndexError):
            pass
    return stats


def show_stats(iface: str) -> dict[str, dict[str, Any]]:
    """Парсить `wg show {iface} dump` → статистика по публичным ключам пиров.

    Возвращает: {pubkey: {endpoint, last_handshake_ts, rx_bytes, tx_bytes}}
    """
    r = subprocess.run(
        ["wg", "show", iface, "dump"],
        capture_output=True, text=True, timeout=_TIMEOUT,
    )
    if r.returncode != 0:
        return {}

    stats: dict[str, dict[str, Any]] = {}
    lines = r.stdout.strip().splitlines()
    # Первая строка — server info, пропускаем
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        peer_pub, _psk, endpoint, _allowed, handshake_ts, rx, tx, *_ = parts
        try:
            stats[peer_pub] = {
                "endpoint": endpoint if endpoint != "(none)" else None,
                "last_handshake_ts": int(handshake_ts),
                "rx_bytes": int(rx),
                "tx_bytes": int(tx),
            }
        except (ValueError, IndexError):
            pass
    return stats


# ---------------------------------------------------------------------------
# Config file generation
# ---------------------------------------------------------------------------

def server_conf(network: dict[str, Any]) -> str:
    """Генерировать серверный wg.conf."""
    net = ipaddress.ip_network(network["subnet"], strict=False)
    prefix = net.prefixlen

    lines = [
        "[Interface]",
        f"PrivateKey = {network['server_private_key']}",
        f"Address = {network['server_ip']}/{prefix}",
        f"ListenPort = {network['port']}",
        "SaveConfig = false",
        "",
    ]
    for client_id, peer in network.get("peers", {}).items():
        lines += [
            "# " + client_id,
            "[Peer]",
            f"PublicKey = {peer['public_key']}",
            f"PresharedKey = {peer['psk']}",
            f"AllowedIPs = {peer['ip']}/32",
            "",
        ]
    return "\n".join(lines)


def client_conf(network: dict[str, Any], peer: dict[str, Any]) -> str:
    """Генерировать клиентский .conf файл."""
    net = ipaddress.ip_network(network["subnet"], strict=False)
    prefix = net.prefixlen

    endpoint = network.get("endpoint") or f"YOUR_SERVER_IP:{network['port']}"
    return "\n".join([
        "[Interface]",
        f"PrivateKey = {peer['private_key']}",
        f"Address = {peer['ip']}/{prefix}",
        f"DNS = {network.get('dns', '1.1.1.1')}",
        "",
        "[Peer]",
        f"PublicKey = {network['server_public_key']}",
        f"PresharedKey = {peer['psk']}",
        f"Endpoint = {endpoint}",
        f"AllowedIPs = {network.get('allowed_ips', '0.0.0.0/0')}",
        f"PersistentKeepalive = {network.get('keepalive', 25)}",
        "",
    ])


def write_conf(network: dict[str, Any]) -> str:
    """Записать серверный конфиг во временный файл, вернуть путь."""
    conf = server_conf(network)
    iface = network["interface"]
    # Предпочитаем /etc/wireguard, fallback на /tmp
    etc = Path("/etc/wireguard")
    if etc.exists() and os.access(str(etc), os.W_OK):
        path = str(etc / f"{iface}.conf")
    else:
        path = f"/tmp/wg-{iface}.conf"
    Path(path).write_text(conf)
    os.chmod(path, 0o600)
    return path


# ---------------------------------------------------------------------------

def _check(result: subprocess.CompletedProcess, cmd: str) -> None:
    if result.returncode != 0:
        raise WgNotAvailable(f"{cmd} failed: {result.stderr.strip()}")
