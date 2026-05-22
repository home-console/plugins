#!/usr/bin/env python3
"""CI-скрипт: опубликовать один плагин в marketplace через /releases/from-git.

Без сторонних зависимостей — только stdlib (urllib), чтобы не тащить pip в CI.

ENV:
    MARKETPLACE_API_URL          — например https://marketplace.homeconsole.su
    MARKETPLACE_PUBLISHER_TOKEN  — bearer-токен publisher-роли
    GITHUB_REPOSITORY            — owner/repo (предустанавливается github actions)
    GITHUB_SHA                   — sha коммита (предустанавливается github actions)

Usage:
    python scripts/publish_plugin.py <plugin_name>
        [--ref <ref>]            (по умолчанию GITHUB_SHA)
        [--channel <channel>]    (по умолчанию stable)
        [--no-force]             (по умолчанию force_replace=true)
        [--create-if-missing]    (если плагин ещё не существует — создать)

Exit code: 0 ок, 1 ошибка.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def http_request(
    url: str,
    *,
    token: str,
    method: str = "POST",
    body: dict | None = None,
) -> tuple[int, dict | str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, raw


def ensure_plugin_exists(
    api_url: str, token: str, name: str, pj: dict
) -> None:
    """Если плагин ещё не зарегистрирован в реестре — создать его."""
    status, _ = http_request(
        f"{api_url}/api/plugins/{name}",
        token=token,
        method="GET",
    )
    if status == 200:
        return
    if status != 404:
        raise SystemExit(f"GET /plugins/{name} вернул {status}, abort")

    body = {
        "name": name,
        "display_name": pj.get("integration_name") or pj.get("name") or name,
        "description": pj.get("description", ""),
        "author": pj.get("author", ""),
        "homepage_url": f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}",
        "category": pj.get("category", "integration"),
        "tags": ",".join(pj.get("tags", [])) if isinstance(pj.get("tags"), list) else "",
    }
    status, payload = http_request(
        f"{api_url}/api/admin/plugins",
        token=token,
        method="POST",
        body=body,
    )
    if status not in (200, 201):
        raise SystemExit(f"POST /admin/plugins failed [{status}]: {payload}")
    print(f"  created plugin '{name}' in registry")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_name")
    parser.add_argument("--ref", default=os.environ.get("GITHUB_SHA", "main"))
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--no-force", action="store_true")
    parser.add_argument("--create-if-missing", action="store_true")
    args = parser.parse_args()

    api_url = os.environ.get("MARKETPLACE_API_URL", "").rstrip("/")
    token = os.environ.get("MARKETPLACE_PUBLISHER_TOKEN", "")
    if not api_url or not token:
        print(
            "ERR: MARKETPLACE_API_URL и MARKETPLACE_PUBLISHER_TOKEN обязательны",
            file=sys.stderr,
        )
        return 2

    plugin_dir = REPO_ROOT / args.plugin_name
    pj_path = plugin_dir / "plugin.json"
    if not pj_path.is_file():
        print(f"ERR: {pj_path} не найден", file=sys.stderr)
        return 2
    pj = json.loads(pj_path.read_text(encoding="utf-8"))

    if args.create_if_missing:
        ensure_plugin_exists(api_url, token, args.plugin_name, pj)

    body = {
        "ref": args.ref,
        "channel": args.channel,
        "subpath": args.plugin_name,
        "force_replace": not args.no_force,
    }
    repo_env = os.environ.get("GITHUB_REPOSITORY", "")
    if repo_env:
        body["source_repo"] = f"https://github.com/{repo_env}"

    url = f"{api_url}/api/plugins/{args.plugin_name}/releases/from-git"
    print(f"POST {url}")
    print(f"  body={json.dumps(body)}")
    status, payload = http_request(url, token=token, method="POST", body=body)

    if 200 <= status < 300:
        if isinstance(payload, dict):
            replaced = payload.get("replaced")
            version = payload.get("version", "?")
            sha = (payload.get("git_sha") or "")[:7]
            print(
                f"OK  {args.plugin_name} v{version} "
                f"{'replaced' if replaced else 'published'} from {sha or args.ref}"
            )
        else:
            print(f"OK  {args.plugin_name} [{status}]")
        return 0

    print(f"FAIL  {args.plugin_name} [{status}]: {payload}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
