from __future__ import annotations

import pytest

from sdk.testing import make_test_context
from plugins.network_scanner.plugin import NetworkScannerPlugin
from plugins.network_scanner.scanner import NetworkHost, NetworkScanner


class TestNetworkHost:
    def test_network_host_creation(self):
        host = NetworkHost(ip_address="192.168.1.100", hostname="test.local", is_online=True)
        assert host.ip_address == "192.168.1.100"
        assert host.hostname == "test.local"
        assert host.is_online is True
        assert host.open_ports == []
        assert host.services == []
        assert host.last_seen is not None

    def test_network_host_to_dict(self):
        host = NetworkHost(ip_address="192.168.1.100", hostname="test.local", is_online=True)
        host_dict = host.to_dict()
        assert host_dict["ip_address"] == "192.168.1.100"
        assert host_dict["hostname"] == "test.local"
        assert host_dict["is_online"] is True


class TestNetworkScanner:
    def test_scanner_initialization(self):
        scanner = NetworkScanner()
        assert scanner.discovered_hosts == {}
        assert len(scanner.get_discovered_hosts()) == 0

    @pytest.mark.asyncio
    async def test_discovery_hosts_cache(self):
        scanner = NetworkScanner()
        host = NetworkHost(ip_address="192.168.1.100")
        scanner.discovered_hosts["192.168.1.100"] = host
        discovered = scanner.get_discovered_hosts()
        assert len(discovered) == 1
        assert discovered[0].ip_address == "192.168.1.100"

    def test_clear_cache(self):
        scanner = NetworkScanner()
        scanner.discovered_hosts["192.168.1.100"] = NetworkHost(ip_address="192.168.1.100")
        scanner.discovered_hosts["192.168.1.101"] = NetworkHost(ip_address="192.168.1.101")
        assert len(scanner.get_discovered_hosts()) == 2
        scanner.clear_discovered_hosts()
        assert len(scanner.get_discovered_hosts()) == 0


class TestNetworkScannerPlugin:
    def test_plugin_metadata(self):
        plugin = NetworkScannerPlugin(make_test_context())
        metadata = plugin.metadata
        assert metadata.name == "network_scanner"
        assert metadata.version == "0.1.0"
        assert metadata.author == "Home Console"
        assert metadata.capabilities_required == []

