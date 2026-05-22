#!/usr/bin/env python3
"""Утилита для CI: вывести список папок верхнего уровня, в которых есть plugin.json
и которые затронуты diff-ом между `base_ref` и `head_ref`.

Используется в .github/workflows/publish.yml и validate.yml.

Usage:
    python scripts/changed_subpaths.py <base_ref> <head_ref>
    python scripts/changed_subpaths.py origin/main HEAD

Вывод: JSON-массив имён папок плагинов (для GitHub Actions matrix).
Пример: ["oauth_yandex", "network_scanner"]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVICE_DIRS = {".github", "scripts", "_example"}


def is_plugin_dir(path: Path) -> bool:
    return path.is_dir() and (path / "plugin.json").is_file()


def all_plugin_dirs() -> list[str]:
    result: list[str] = []
    for entry in sorted(REPO_ROOT.iterdir()):
        if entry.name.startswith(".") or entry.name in SERVICE_DIRS:
            continue
        if is_plugin_dir(entry):
            result.append(entry.name)
    return result


def changed_files(base_ref: str, head_ref: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...{head_ref}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: changed_subpaths.py <base_ref> <head_ref>",
            file=sys.stderr,
        )
        return 2
    base_ref, head_ref = sys.argv[1], sys.argv[2]

    if os.getenv("PUBLISH_ALL") == "1":
        plugins = all_plugin_dirs()
    else:
        try:
            files = changed_files(base_ref, head_ref)
        except subprocess.CalledProcessError as exc:
            print(f"git diff failed: {exc.stderr}", file=sys.stderr)
            return 1

        touched: set[str] = set()
        for f in files:
            parts = f.split("/", 1)
            if not parts:
                continue
            top = parts[0]
            if top.startswith(".") or top in SERVICE_DIRS:
                continue
            if is_plugin_dir(REPO_ROOT / top):
                touched.add(top)
        plugins = sorted(touched)

    json.dump(plugins, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
