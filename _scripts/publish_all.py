#!/usr/bin/env python3
"""Опубликовать все плагины из репозитория plugins/ в marketplace.

Обёртка над publish_plugin.py — по очереди вызывает publish для каждой
папки верхнего уровня с plugin.json (кроме служебных _*).

ENV:
    MARKETPLACE_API_URL
    MARKETPLACE_PUBLISHER_TOKEN
    GITHUB_REPOSITORY   (опционально, для source_repo)
    GITHUB_SHA          (опционально, ref по умолчанию)

Usage:
    python _scripts/publish_all.py
    python _scripts/publish_all.py --channel dev --no-force
    python _scripts/publish_all.py --only alerting webhook
    python _scripts/publish_all.py --dry-run

Exit code: 0 если все успешны, 1 если хотя бы один провалился.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISH_SCRIPT = Path(__file__).resolve().parent / "publish_plugin.py"


def all_plugin_names() -> list[str]:
    names: list[str] = []
    for entry in sorted(REPO_ROOT.iterdir()):
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        if entry.is_dir() and (entry / "plugin.json").is_file():
            names.append(entry.name)
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish all plugins to marketplace")
    parser.add_argument("--ref", default=None, help="Git ref (default: GITHUB_SHA or main)")
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--no-force", action="store_true")
    parser.add_argument(
        "--create-if-missing",
        action="store_true",
        default=True,
        help="Создать карточку плагина если её ещё нет (default: on)",
    )
    parser.add_argument(
        "--no-create-if-missing",
        action="store_false",
        dest="create_if_missing",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="PLUGIN",
        help="Опубликовать только указанные плагины",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать список плагинов, без публикации",
    )
    args = parser.parse_args()

    plugins = args.only if args.only else all_plugin_names()
    if not plugins:
        print("Нет плагинов для публикации", file=sys.stderr)
        return 2

    print(f"Плагинов к публикации: {len(plugins)}")
    for name in plugins:
        print(f"  - {name}")

    if args.dry_run:
        return 0

    failed: list[str] = []
    for name in plugins:
        cmd = [sys.executable, str(PUBLISH_SCRIPT), name, "--channel", args.channel]
        if args.ref:
            cmd.extend(["--ref", args.ref])
        if args.no_force:
            cmd.append("--no-force")
        if args.create_if_missing:
            cmd.append("--create-if-missing")

        print(f"\n--- {name} ---")
        result = subprocess.run(cmd, cwd=REPO_ROOT)
        if result.returncode != 0:
            failed.append(name)

    if failed:
        print(f"\nFAIL: не опубликованы: {', '.join(failed)}", file=sys.stderr)
        return 1

    print(f"\nOK: все {len(plugins)} плагинов опубликованы")
    return 0


if __name__ == "__main__":
    sys.exit(main())
