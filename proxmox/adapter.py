"""ProxmoxCommandAdapter — pvesh команды для агентов на Proxmox-узлах."""
from __future__ import annotations

import json
from typing import Any

from client_manager_plugin_app.core.commands.base import (
    CommandAdapter, CommandDescriptor, UniversalCommand,
)

_COMMANDS = [
    CommandDescriptor("vm.list",          "Список виртуальных машин"),
    CommandDescriptor("vm.start",         "Запустить VM",          params_schema={"vmid": {"type": "integer"}}),
    CommandDescriptor("vm.stop",          "Остановить VM",         params_schema={"vmid": {"type": "integer"}}),
    CommandDescriptor("vm.status",        "Статус VM",             params_schema={"vmid": {"type": "integer"}}),
    CommandDescriptor("ct.list",          "Список LXC-контейнеров"),
    CommandDescriptor("ct.start",         "Запустить контейнер",   params_schema={"ctid": {"type": "integer"}}),
    CommandDescriptor("ct.stop",          "Остановить контейнер",  params_schema={"ctid": {"type": "integer"}}),
    CommandDescriptor("node.status",      "Статус PVE-узла"),
    CommandDescriptor("storage.pools",    "Список хранилищ"),
    CommandDescriptor("backup.create",    "Создать бэкап",         params_schema={"vmid": {"type": "integer"}, "storage": {"type": "string"}}),
]


class ProxmoxCommandAdapter(CommandAdapter):
    """Транслирует UniversalCommand в pvesh команды для выполнения на агенте."""

    @property
    def commands(self) -> list[CommandDescriptor]:
        return _COMMANDS

    def adapt(self, command: UniversalCommand) -> str:
        p = command.params or {}
        ct = command.command_type

        if ct == "vm.list":
            return "pvesh get /nodes/$(hostname)/qemu --output-format json"
        if ct == "vm.start":
            return f"pvesh create /nodes/$(hostname)/qemu/{p.get('vmid', '')}/status/start"
        if ct == "vm.stop":
            return f"pvesh create /nodes/$(hostname)/qemu/{p.get('vmid', '')}/status/stop"
        if ct == "vm.status":
            return f"pvesh get /nodes/$(hostname)/qemu/{p.get('vmid', '')}/status/current --output-format json"
        if ct == "ct.list":
            return "pvesh get /nodes/$(hostname)/lxc --output-format json"
        if ct == "ct.start":
            return f"pvesh create /nodes/$(hostname)/lxc/{p.get('ctid', '')}/status/start"
        if ct == "ct.stop":
            return f"pvesh create /nodes/$(hostname)/lxc/{p.get('ctid', '')}/status/stop"
        if ct == "node.status":
            return "pvesh get /nodes/$(hostname)/status --output-format json"
        if ct == "storage.pools":
            return "pvesh get /nodes/$(hostname)/storage --output-format json"
        if ct == "backup.create":
            vmid    = p.get("vmid", "")
            storage = p.get("storage", "local")
            return f"pvesh create /nodes/$(hostname)/vzdump --vmid {vmid} --storage {storage} --mode snapshot"
        return f"echo 'unsupported proxmox command: {ct}'"

    def parse_result(self, command: UniversalCommand, result: Any) -> dict[str, Any]:
        raw = str(result) if result is not None else ""
        try:
            return {"data": json.loads(raw), "raw": raw}
        except (json.JSONDecodeError, ValueError):
            return {"raw": raw}
