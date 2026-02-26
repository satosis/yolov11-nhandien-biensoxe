#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import socket
from urllib.parse import urlparse


def load_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def is_loopback_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in {"127.0.0.1", "localhost"}
    except Exception:
        return False


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Check HA remote access prerequisites")
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()

    if not os.path.exists(args.env_file):
        raise SystemExit(f"env file not found: {args.env_file}")

    env = load_env(args.env_file)
    internal = env.get("HA_INTERNAL_URL", "")
    external = env.get("HA_EXTERNAL_URL", "")
    ts_auth = env.get("TS_AUTHKEY", "")
    ts_hostname = env.get("TS_HOSTNAME", "ha-gateway")

    print("=== Home Assistant remote access check ===")
    print(f"HA_INTERNAL_URL: {internal or '(missing)'}")
    print(f"HA_EXTERNAL_URL: {external or '(missing)'}")
    print(f"TS_HOSTNAME: {ts_hostname}")
    print(f"TS_AUTHKEY set: {'yes' if bool(ts_auth.strip()) else 'no'}")

    if not internal:
        print("[WARN] Missing HA_INTERNAL_URL")
    if not external:
        print("[WARN] Missing HA_EXTERNAL_URL")

    if internal and is_loopback_host(internal):
        print("[WARN] HA_INTERNAL_URL đang dùng localhost/127.0.0.1 (chỉ truy cập được trên chính máy chạy HA)")
    if external and is_loopback_host(external):
        print("[WARN] HA_EXTERNAL_URL đang dùng localhost/127.0.0.1 nên không truy cập được từ mạng khác")

    if external:
        parsed = urlparse(external)
        if not parsed.scheme or not parsed.netloc:
            print("[WARN] HA_EXTERNAL_URL is not a valid URL")
        elif parsed.scheme not in {"http", "https"}:
            print("[WARN] HA_EXTERNAL_URL should start with http:// or https://")

    # Best-effort local health check for HA
    if port_open("127.0.0.1", 8123):
        print("[OK] Local Home Assistant port 8123 is reachable")
    else:
        print("[WARN] Local Home Assistant port 8123 is not reachable")

    if ts_auth.strip():
        print("[INFO] TS_AUTHKEY is set. Start Tailscale with:")
        print("       ./cmd remote-up")

    if not ts_auth.strip():
        print("[INFO] TS_AUTHKEY is empty: Tailscale remote access profile cannot authenticate yet")
        print("       Add TS_AUTHKEY in .env then run:")
        print("       docker compose --profile remote_ha_tailscale up -d tailscale")

    if external and re.search(r"\.ts\.net(?::\d+)?$", urlparse(external).netloc):
        print("[OK] HA_EXTERNAL_URL looks like Tailscale MagicDNS")
    elif external:
        print("[INFO] HA_EXTERNAL_URL is not MagicDNS. Ensure your tunnel/VPN/domain routes to this host.")


if __name__ == "__main__":
    main()
