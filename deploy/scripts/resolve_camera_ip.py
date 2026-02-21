#!/usr/bin/env python3
"""Resolve CAMERA_IP from CAMERA_MAC and write a runtime env file."""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit


MAC_RE = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")
IP_RE = re.compile(r"\b(\d+\.\d+\.\d+\.\d+)\b")


def normalize_mac(value: str) -> str:
    value = value.strip().lower().replace("-", ":")
    if not MAC_RE.fullmatch(value):
        raise ValueError(f"Invalid MAC address: {value}")
    return value


def load_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def parse_env(lines: list[str]) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        data[key] = value
    return data


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def find_ip_for_mac(mac: str) -> str | None:
    outputs = [run(["ip", "neigh"]), run(["arp", "-an"]), run(["cat", "/proc/net/arp"])]
    patterns = [
        re.compile(r"(?P<ip>\d+\.\d+\.\d+\.\d+).*\blladdr\s+(?P<mac>[0-9a-f:]{17})", re.I),
        re.compile(r"\((?P<ip>\d+\.\d+\.\d+\.\d+)\)\s+at\s+(?P<mac>[0-9a-f:]{17})", re.I),
        re.compile(r"(?P<ip>\d+\.\d+\.\d+\.\d+)\s+\S+\s+\S+\s+(?P<mac>[0-9a-f:]{17})", re.I),
    ]

    for output in outputs:
        for line in output.splitlines():
            for pattern in patterns:
                m = pattern.search(line)
                if not m:
                    continue
                row_mac = m.group("mac").lower().replace("-", ":")
                if row_mac == mac:
                    return m.group("ip")
    return None


def parse_ipv4_network(candidate: str) -> ipaddress.IPv4Network | None:
    try:
        return ipaddress.ip_network(candidate, strict=False)
    except ValueError:
        return None


def network_from_ip(ip_text: str) -> ipaddress.IPv4Network | None:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return None
    if not isinstance(ip, ipaddress.IPv4Address):
        return None
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def discover_candidate_networks(data: dict[str, str]) -> list[ipaddress.IPv4Network]:
    candidates: list[ipaddress.IPv4Network] = []

    # 1) user-provided subnet has highest priority
    subnet = data.get("CAMERA_IP_SUBNET", "").strip()
    if subnet:
        network = parse_ipv4_network(subnet)
        if network:
            candidates.append(network)

    # 2) current IPs on physical interfaces (prefer private LAN over tailscale/docker)
    addr_output = run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    ignored_if_prefixes = ("docker", "br-", "veth", "tailscale", "zt", "tun", "wg", "lo")
    physical: list[ipaddress.IPv4Network] = []
    fallback: list[ipaddress.IPv4Network] = []

    for line in addr_output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1]
        cidr = parts[3]
        network = parse_ipv4_network(cidr)
        if not network:
            continue
        fallback.append(network)
        if iface.startswith(ignored_if_prefixes):
            continue
        if network.network_address.is_private:
            physical.append(network)

    candidates.extend(physical or fallback)

    # 3) derive /24 from host in RTSP_URL (legacy static URL)
    rtsp_url = data.get("RTSP_URL", "").strip()
    if rtsp_url:
        host = urlsplit(rtsp_url).hostname
        if host:
            network = network_from_ip(host)
            if network:
                candidates.append(network)

    # 4) default route source as last resort
    route_output = run(["ip", "-4", "route", "show", "default"])
    for ip_text in IP_RE.findall(route_output):
        network = network_from_ip(ip_text)
        if network:
            candidates.append(network)

    # dedupe while preserving order
    unique: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    for network in candidates:
        key = str(network)
        if key in seen:
            continue
        seen.add(key)
        unique.append(network)
    return unique


def touch_hosts(network: ipaddress.IPv4Network) -> None:
    hosts = [str(ip) for ip in network.hosts()]

    def hit(ip: str) -> None:
        # ping is more reliable than TCP 554 probing for ARP refresh.
        run(["ping", "-c", "1", "-W", "1", ip])

    with ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(hit, hosts))


def update_or_insert(lines: list[str], key: str, value: str) -> list[str]:
    new_line = f"{key}={value}"
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[idx] = new_line
            return lines
    lines.append(new_line)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env", help="Source env file (contains CAMERA_MAC)")
    parser.add_argument("--out-env-file", default="", help="Target env file for resolved CAMERA_IP")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    env_path = Path(args.env_file)
    out_env_path = Path(args.out_env_file) if args.out_env_file else env_path

    lines = load_env_lines(env_path)
    data = parse_env(lines)

    camera_mac = data.get("CAMERA_MAC", "").strip()
    if not camera_mac:
        if not args.quiet:
            print("[camera-ip] CAMERA_MAC is empty; skip auto resolution")
        return 0

    try:
        camera_mac = normalize_mac(camera_mac)
    except ValueError as exc:
        print(f"[camera-ip] {exc}")
        return 1

    camera_ip = find_ip_for_mac(camera_mac)
    if not camera_ip:
        networks = discover_candidate_networks(data)
        for network in networks:
            if not args.quiet:
                print(f"[camera-ip] scanning {network} to refresh ARP cache...")
            touch_hosts(network)
            camera_ip = find_ip_for_mac(camera_mac)
            if camera_ip:
                break

    if not camera_ip:
        print("[camera-ip] Cannot resolve CAMERA_IP from CAMERA_MAC.")
        print("[camera-ip] Ensure camera is connected to same LAN.")
        print("[camera-ip] Tip: set CAMERA_IP_SUBNET in .env (e.g. 10.115.215.0/24) to improve discovery.")
        return 1

    out_lines = load_env_lines(out_env_path)
    out_lines = update_or_insert(out_lines, "CAMERA_IP", camera_ip)
    out_env_path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    if not args.quiet:
        print(f"[camera-ip] CAMERA_IP={camera_ip} written to {out_env_path} (from MAC {camera_mac})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
