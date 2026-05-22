#!/usr/bin/env python3
"""Локально/в CI проверить один плагин: схема plugin.json + наличие class_path-модуля.

Usage:
    python scripts/validate_plugin.py <plugin_dir>
    python scripts/validate_plugin.py oauth_yandex

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

REQUIRED_FIELDS = {"name", "version", "description", "class_path", "role"}
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
        if len(parts) < 3 or parts[0] != "plugins" or parts[1] != name:
            err(
                f"plugin.json.class_path='{class_path}' должно начинаться с "
                f"'plugins.{name}.'",
                errors,
            )
        else:
            module_rel = Path(*parts[2:-1]).with_suffix(".py")
            module_path = plugin_dir / module_rel
            if not module_path.is_file():
                err(
                    f"class_path указывает на отсутствующий модуль: {module_path}",
                    errors,
                )

    deps = data.get("dependencies", [])
    if not isinstance(deps, list):
        err("plugin.json.dependencies должен быть массивом строк", errors)

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
