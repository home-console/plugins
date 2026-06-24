#!/usr/bin/env python3
"""Локально/в CI проверить один плагин: схема plugin.json + наличие class_path-модуля.

Usage:
    python _scripts/validate_plugin.py <plugin_dir>
    python _scripts/validate_plugin.py oauth_yandex

Exit code:
    0 — ок
    1 — ошибки валидации
    2 — usage error
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FIELDS = {"name", "version", "description", "author", "class_path"}
CONTRACT_KEYS = {
    "consumes_services",
    "subscribes_events",
    "capabilities_required",
    "provides_services",
    "provides_events",
    "capabilities_provided",
}
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")
ALLOWED_ROLES = {"capability_provider", "integration", "util", "core_extension"}


def err(msg: str, errors: list[str]) -> None:
    errors.append(msg)


def validate(plugin_dir: Path) -> list[str]:
    errors: list[str] = []
    if not plugin_dir.is_dir():
        err(f"{plugin_dir}: не каталог", errors)
        return errors

    pj_path = plugin_dir / "plugin.json"
    if not pj_path.is_file():
        err(f"{plugin_dir}: нет plugin.json", errors)
        return errors

    try:
        data = json.loads(pj_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err(f"plugin.json: невалидный JSON ({exc})", errors)
        return errors

    if not isinstance(data, dict):
        err("plugin.json: ожидался object", errors)
        return errors

    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        err(f"plugin.json: отсутствуют обязательные поля: {sorted(missing)}", errors)

    name = data.get("name", "")
    if not isinstance(name, str) or not NAME_RE.match(name):
        err(
            f"plugin.json.name='{name}' должно быть snake_case по [a-z_][a-z0-9_]*",
            errors,
        )
    elif name != plugin_dir.name:
        err(
            f"plugin.json.name='{name}' не совпадает с именем папки '{plugin_dir.name}'",
            errors,
        )

    version = data.get("version", "")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        err(f"plugin.json.version='{version}' не похоже на semver (например 0.1.0)", errors)

    role = data.get("role", "")
    if role and role not in ALLOWED_ROLES:
        err(
            f"plugin.json.role='{role}' не из {sorted(ALLOWED_ROLES)}",
            errors,
        )

    class_path = data.get("class_path", "")
    if class_path:
        parts = class_path.split(".")
        if len(parts) < 2:
            err(
                f"plugin.json.class_path='{class_path}' должен быть в формате "
                f"'<module>.<Class>' или 'plugins.{name}.<module>.<Class>'",
                errors,
            )
        else:
            # Поддерживаем два формата:
            #   1) plugins.<name>.<module>[...].<Class>   (абсолютный от корня core)
            #   2) <module>[...].<Class>                  (относительный от папки плагина)
            if parts[0] == "plugins":
                if len(parts) < 4 or parts[1] != name:
                    err(
                        f"plugin.json.class_path='{class_path}' начинается с 'plugins.', "
                        f"но дальше должно идти '{name}.<module>.<Class>'",
                        errors,
                    )
                    module_parts: list[str] = []
                else:
                    module_parts = parts[2:-1]
            else:
                module_parts = parts[:-1]

            if module_parts:
                module_rel = Path(*module_parts).with_suffix(".py")
                module_path = plugin_dir / module_rel
                if not module_path.is_file():
                    err(
                        f"class_path указывает на отсутствующий модуль: {module_path}",
                        errors,
                    )

    deps = data.get("dependencies", [])
    if not isinstance(deps, list):
        err("plugin.json.dependencies должен быть массивом строк", errors)

    contract = data.get("contract")
    if contract is None:
        err("plugin.json: отсутствует секция contract (обязательна с manifest 2.0)", errors)
    elif not isinstance(contract, dict):
        err("plugin.json.contract должен быть object", errors)
    else:
        missing_contract = CONTRACT_KEYS - set(contract.keys())
        if missing_contract:
            err(
                f"plugin.json.contract: отсутствуют ключи: {sorted(missing_contract)}",
                errors,
            )
        for key in CONTRACT_KEYS:
            val = contract.get(key)
            if val is not None and not isinstance(val, list):
                err(f"plugin.json.contract.{key} должен быть массивом", errors)

    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_plugin.py <plugin_dir>", file=sys.stderr)
        return 2

    arg = sys.argv[1]
    plugin_dir = (REPO_ROOT / arg).resolve()

    errors = validate(plugin_dir)
    if errors:
        print(f"FAIL  {plugin_dir.name}")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"OK    {plugin_dir.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
