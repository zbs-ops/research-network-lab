"""Part D client: fixed-rate writes and a linearizable read-back, over etcd's JSON gateway.

Writes are open-loop: write k is sent at start + k/rate whether or not earlier writes have
finished, so an outage shows up as failed or slow writes instead of silently lowering the rate.
Each write is one HTTP/1.1 request on its own connection to one fixed member, with no retries,
so every write has exactly one outcome:

    ok       200 from etcd: the write is acknowledged (committed)
    timeout  no answer within the client timeout: outcome unknown
    err:...  etcd answered with an error (e.g. 'etcdserver: request timed out')
    conn     the connection failed

Python standard library only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
import urllib.request


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


async def _post(host: str, port: int, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(
            f"POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        await writer.drain()
        raw = await reader.read()
    finally:
        writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    status = int(lines[0].split()[1])
    if any(h.lower().startswith("transfer-encoding:") and "chunked" in h.lower() for h in lines[1:]):
        payload = _dechunk(payload)
    try:
        return status, json.loads(payload or b"{}")
    except json.JSONDecodeError:
        return status, {"error": payload[:200].decode(errors="replace")}


def _dechunk(b: bytes) -> bytes:
    out = b""
    while b:
        size_line, _, rest = b.partition(b"\r\n")
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            break
        out += rest[:size]
        b = rest[size + 2:]
    return out


async def _one_write(host: str, key: str, value: str, timeout: float, t0: float) -> list:
    t_send = time.time()
    try:
        status, body = await asyncio.wait_for(
            _post(host, 2379, "/v3/kv/put", {"key": b64(key), "value": b64(value)}), timeout)
        outcome = "ok" if status == 200 and "error" not in body else "err:" + str(body.get("error", status))[:80]
    except asyncio.TimeoutError:
        outcome = "timeout"
    except OSError:
        outcome = "conn"
    return [round(t_send - t0, 4), round(time.time() - t0, 4), outcome]  # 0.1 ms resolution


async def write_stream(host: str, prefix: str, rate: float, duration: float, timeout: float,
                       t0: float) -> tuple[list[list], dict[str, str]]:
    """Send rate*duration writes to host, starting at t0. Keys are '<prefix>/<seq>'.

    Returns (writes, values): writes are [seq, t_send, t_done, outcome] with times relative to
    t0; values maps every key sent to its unique value.
    """
    n = int(rate * duration)
    tasks, values = [], {}
    tag = secrets.token_hex(4)
    for k in range(n):
        delay = t0 + k / rate - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        key = f"{prefix}/{k:06d}"
        values[key] = f"{tag}-{k}"
        tasks.append(asyncio.create_task(_one_write(host, key, values[key], timeout, t0)))
    done = await asyncio.gather(*tasks)
    return [[k] + d for k, d in enumerate(done)], values


def read_prefix(url: str, prefix: str, serializable: bool = False, timeout: float = 5.0) -> dict[str, str]:
    """Every key under prefix/ with its value. Linearizable unless serializable=True."""
    # [prefix/, prefix0): '0' is the byte after '/', so this is exactly the keys under prefix/.
    body = {"key": b64(prefix + "/"), "range_end": b64(prefix + "0")}
    if serializable:
        body["serializable"] = True
    req = urllib.request.Request(url + "/v3/kv/range", data=json.dumps(body).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if "error" in d:
        raise RuntimeError(d["error"])
    return {base64.b64decode(kv["key"]).decode(): base64.b64decode(kv.get("value", "")).decode()
            for kv in d.get("kvs", [])}
