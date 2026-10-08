#!/usr/bin/env python3
import base64
import hashlib
import ipaddress
import json
import os
import random
import socket
import ssl
import struct
import time
import urllib.request
import urllib.parse
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

CF_RANGES_URL = "https://www.cloudflare.com/ips-v4"


def getenv(name: str, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        raise RuntimeError(f"missing environment variable: {name}")
    return v


def load_config():
    with open(os.path.join(os.path.dirname(__file__), "config.example.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def http_get_text(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "CF-EDT-Cloud-Auto/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def choose_ips(n, seed):
    raw = http_get_text(CF_RANGES_URL)
    networks = [ipaddress.ip_network(x.strip()) for x in raw.splitlines() if x.strip()]
    subnets = []
    for net in networks:
        if net.num_addresses <= 256:
            subnets.append(net)
        else:
            subnets.extend(net.subnets(new_prefix=24))
    rng = random.Random(seed)
    if len(subnets) <= n:
        picked = subnets
    else:
        picked = rng.sample(subnets, n)
    ips = []
    for sn in picked:
        # 每个 /24 选一个非网络地址；尽量让每日抽样变化但可复现
        hosts = list(sn.hosts())
        if not hosts:
            continue
        ips.append(str(rng.choice(hosts)))
    return sorted(set(ips))


def recv_until(sock, marker=b"\r\n\r\n", limit=65536):
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise RuntimeError("HTTP header too large")
    return data


def parse_headers(data: bytes):
    head = data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
    lines = head.split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.lower().strip()] = v.strip()
    return status, headers


def tls_conn(ip: str, host: str, port: int, timeout=4):
    raw = socket.create_connection((ip, port), timeout=timeout)
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    ss = ctx.wrap_socket(raw, server_hostname=host)
    ss.settimeout(timeout)
    return ss


def http_probe(ip, cfg):
    t0 = time.perf_counter()
    try:
        s = tls_conn(ip, cfg["host"], cfg["port"], timeout=4)
        req = (
            f"GET / HTTP/1.1\r\nHost: {cfg['host']}\r\nUser-Agent: CF-EDT-Cloud-Auto/1.0\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        s.sendall(req)
        head = recv_until(s)
        status, headers = parse_headers(head)
        latency = (time.perf_counter() - t0) * 1000
        ray = headers.get("cf-ray", "")
        colo = ray.rsplit("-", 1)[-1].upper() if "-" in ray else ""
        s.close()
        if status >= 400:
            return None
        if cfg["colo"] and colo != cfg["colo"]:
            return None
        return {"ip": ip, "latency": latency, "colo": colo}
    except Exception:
        return None


def ws_send(sock, payload: bytes, opcode=2):
    key = os.urandom(4)
    first = 0x80 | opcode
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", first, 0x80 | n)
    elif n <= 65535:
        header = struct.pack("!BBH", first, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", first, 0x80 | 127, n)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + key + masked)


def ws_recv(sock):
    h = sock.recv(2)
    if len(h) != 2:
        return None, None
    fin = bool(h[0] & 0x80)
    opcode = h[0] & 0x0F
    masked = bool(h[1] & 0x80)
    n = h[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", sock.recv(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", sock.recv(8))[0]
    if n > 16 * 1024 * 1024:
        raise RuntimeError("WS frame too large")
    if masked:
        mask = sock.recv(4)
    data = b""
    while len(data) < n:
        chunk = sock.recv(min(65536, n - len(data)))
        if not chunk:
            raise RuntimeError("WS closed")
        data += chunk
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def vless_request(uuid_text, target_host, target_port):
    raw_uuid = bytes.fromhex(uuid_text.replace("-", ""))
    if len(raw_uuid) != 16:
        raise ValueError("VLESS UUID invalid")
    host_b = target_host.encode()
    # 0x01 = TCP, 0x02 = UDP; 0x02 address type = domain
    return bytes([1]) + raw_uuid + bytes([0, 1]) + struct.pack("!H", target_port) + bytes([2, len(host_b)]) + host_b


def vless_probe(item, cfg, uuid_text):
    ip = item["ip"]
    t0 = time.perf_counter()
    s = None
    try:
        s = tls_conn(ip, cfg["host"], cfg["port"], timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        path = cfg["path"]
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {cfg['host']}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: Mozilla/5.0\r\n\r\n"
        ).encode()
        s.sendall(req)
        header = recv_until(s)
        status, headers = parse_headers(header)
        if status != 101:
            raise RuntimeError(f"WS HTTP {status}")

        ws_send(s, vless_request(uuid_text, cfg["speed_host"], 80), opcode=2)
        deadline = time.time() + 5
        data = b""
        while time.time() < deadline and len(data) < 2:
            s.settimeout(max(0.2, deadline - time.time()))
            op, payload = ws_recv(s)
            if op == 9:
                ws_send(s, payload, opcode=10)
                continue
            if op == 8:
                raise RuntimeError("WS close after VLESS request")
            if op == 2:
                data += payload
        if len(data) < 2 or data[:1] != b"\x01":
            raise RuntimeError("VLESS response header invalid")

        http_get = (
            f"GET {cfg['speed_path']} HTTP/1.1\r\nHost: {cfg['speed_host']}\r\nUser-Agent: CF-EDT-Cloud-Auto/1.0\r\n"
            "Connection: close\r\nAccept-Encoding: identity\r\n\r\n"
        ).encode()
        ws_send(s, http_get, opcode=2)

        raw = b""
        body = 0
        header_done = False
        measure_start = time.perf_counter()
        while time.perf_counter() - measure_start < cfg["speed_seconds"]:
            s.settimeout(1.0)
            try:
                op, payload = ws_recv(s)
            except socket.timeout:
                continue
            if op == 9:
                ws_send(s, payload, opcode=10)
                continue
            if op in (8, None):
                break
            if op != 2:
                continue
            raw += payload
            if not header_done:
                marker = raw.find(b"\r\n\r\n")
                if marker >= 0:
                    header_done = True
                    body = len(raw) - marker - 4
                    raw = b""
            elif len(payload):
                body += len(payload)
        elapsed = max(0.2, time.perf_counter() - measure_start)
        speed = body / elapsed / 1024 / 1024
        item = dict(item)
        item["speed"] = speed
        item["full_latency"] = (time.perf_counter() - t0) * 1000
        return item if speed >= cfg["min_speed_MBps"] else None
    except Exception:
        return None
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def edt_login(host, password):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    data = urllib.parse.urlencode({"password": password}).encode()
    req = urllib.request.Request(host.rstrip("/") + "/login", data=data, method="POST", headers={"Content-Type":"application/x-www-form-urlencoded"})
    with opener.open(req, timeout=15) as r:
        body = r.read().decode("utf-8", "replace")
        if r.status != 200 or "success" not in body.lower():
            raise RuntimeError("EDT login failed")
    return opener


def edt_read(opener, host):
    req = urllib.request.Request(host.rstrip("/") + f"/admin/ADD.txt?_t={int(time.time())}", headers={"Cache-Control":"no-cache"})
    with opener.open(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace").strip()


def edt_write(opener, host, content):
    req = urllib.request.Request(host.rstrip("/") + "/admin/ADD.txt", data=content.encode(), method="POST", headers={"Content-Type":"text/plain"})
    with opener.open(req, timeout=15) as r:
        body = r.read().decode("utf-8", "replace")
        if r.status != 200 or '"success":true' not in body.replace(' ', '').lower():
            raise RuntimeError("EDT upload failed: " + body[:300])


def build_add(results):
    return "\n".join(f"{x['ip']}:{x['port']}#NRT" for x in results) + "\n"


def main():
    cfg = load_config()
    host = cfg["host"]
    password = getenv("EDT_PASSWORD")
    uuid_text = getenv("VLESS_UUID")
    edt_url = getenv("EDT_URL")
    day = time.strftime("%Y-%m-%d", time.gmtime())
    print(f"[1/5] sampling Cloudflare IPv4 ranges: {day}")
    ips = choose_ips(cfg["sample_subnets"], day)
    print(f"candidate IPs: {len(ips)}")

    print("[2/5] TLS/HTTP + CF-RAY/Colo filtering")
    prelim = []
    with ThreadPoolExecutor(max_workers=cfg["http_workers"]) as ex:
        futs = [ex.submit(http_probe, ip, cfg) for ip in ips]
        for fut in as_completed(futs):
            r = fut.result()
            if r and r["latency"] <= cfg["max_latency_ms"]:
                r["port"] = cfg["port"]
                prelim.append(r)
    prelim.sort(key=lambda x: x["latency"])
    print(f"NRT under {cfg['max_latency_ms']}ms: {len(prelim)}")
    prelim = prelim[:cfg["vless_candidates"]]

    print("[3/5] real VLESS + WS + TLS speed test")
    qualified = []
    with ThreadPoolExecutor(max_workers=cfg["vless_workers"]) as ex:
        futs = [ex.submit(vless_probe, r, cfg, uuid_text) for r in prelim]
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                qualified.append(r)
                print(f"PASS {r['ip']} latency={r['latency']:.1f}ms speed={r['speed']:.2f}MB/s")
    qualified.sort(key=lambda x: (-x["speed"], x["latency"]))
    qualified = qualified[:cfg["keep"]]
    print(f"qualified: {len(qualified)}")

    print("[4/5] safety check")
    opener = edt_login(edt_url, password)
    old = edt_read(opener, edt_url)
    if len(qualified) < cfg["safe_min_results"]:
        print(f"Only {len(qualified)} qualified IPs; keep remote ADD.txt unchanged.")
        return

    add = build_add(qualified)
    print(add)
    print("[5/5] updating EDT ADD.txt")
    edt_write(opener, edt_url, add)

    with open("latest_ADD.txt", "w", encoding="utf-8") as f:
        f.write(add)
    with open("latest_results.json", "w", encoding="utf-8") as f:
        json.dump(qualified, f, ensure_ascii=False, indent=2)
    print("EDT update completed.")


if __name__ == "__main__":
    main()
