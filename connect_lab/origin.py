"""Owned HTTPS defender with a separate authenticated loopback control API."""
import argparse
import asyncio
import base64
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import ipaddress
import json
from pathlib import Path
import secrets
import signal
import ssl
import time

from .proxy import JsonLog, TOKEN
from .netstats import snapshot


class Origin:
    def __init__(self, config, log, classifier=None):
        self.config, self.log = config, log
        self.network = ipaddress.ip_network(config["source_prefix"])
        self.classifier = classifier or self.classify
        self.secret = secrets.token_bytes(32)
        self.token = Path(config["admin_token_file"]).read_text().strip()
        if len(self.token) < 32:
            raise ValueError("control token must contain at least 32 characters")
        self.servers, self.tasks = [], set()
        self.rows, self.counts = [], {}
        self.row_base = 0
        self.epoch = 0
        self.active = 0
        self.policy = None  # Fail closed until the orchestrator configures a cell.

    def classify(self, source):
        address = ipaddress.ip_address(source)
        if address not in self.network:
            return None
        field = (int(address) >> 48) & 0xffff
        return {0xc: "clean", 0xd: "dirty"}.get(field)

    def set_policy(self, value):
        if self.active:
            raise ValueError("cannot switch policy during a response")
        if value["defender"] not in ("login", "every", "bind"):
            raise ValueError("unknown defender")
        for key in ("body_bytes", "manifest_bytes", "window_s"):
            if not isinstance(value[key], int) or not 0 < value[key] <= 1_000_000_000:
                raise ValueError("invalid " + key)
        if not isinstance(value["limit"], int) or value["limit"] < 0:
            raise ValueError("invalid limit")
        if not TOKEN.fullmatch(value["cell_id"]):
            raise ValueError("invalid cell_id")
        self.policy = dict(value)
        self.epoch += 1
        self.counts.clear()
        self.log({"event": "policy", "epoch": self.epoch, "policy": value, "time": time.time()})
        return {"epoch": self.epoch, "cursor": self.row_base + len(self.rows)}

    def sign(self, payload):
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        signature = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + "." + signature

    def verify(self, cookie, run_id, session_id):
        try:
            jar = SimpleCookie(cookie)
            encoded, signature = jar["lab_session"].value.rsplit(".", 1)
            expected = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(encoded))
            if payload["epoch"] != self.epoch or payload["run"] != run_id or payload["session"] != session_id:
                return None
            return payload
        except (ValueError, KeyError, TypeError, CookieError):
            return None

    async def start(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.config["cert_file"], self.config["key_file"])
        context.set_alpn_protocols(["http/1.1"])
        try:
            self.servers.append(await asyncio.start_server(
                self.handle_data, self.config["listen_ip"], self.config["port"],
                ssl=context, ssl_handshake_timeout=10, limit=16384))
            self.servers.append(await asyncio.start_server(
                self.handle_admin, "127.0.0.1", self.config["admin_port"], limit=16384))
        except BaseException:
            await self.close()
            raise
        return {"https": self.servers[0].sockets[0].getsockname()[1],
                "admin": self.servers[1].sockets[0].getsockname()[1]}

    async def read_request(self, reader):
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 40)
        lines = raw.decode("ascii").split("\r\n")
        method, path, version = lines[0].split()
        if version not in ("HTTP/1.0", "HTTP/1.1"):
            raise ValueError("HTTP/1.x required")
        headers = {}
        for line in lines[1:-2]:
            key, value = line.split(":", 1)
            if key.lower() in headers:
                raise ValueError("duplicate header")
            headers[key.lower()] = value.strip()
        if "transfer-encoding" in headers:
            raise ValueError("chunked requests not supported")
        length = int(headers.get("content-length", "0"))
        if not 0 <= length <= 16384:
            raise ValueError("request body too large")
        body = await asyncio.wait_for(reader.readexactly(length), 10)
        return method, path, headers, body

    async def reply(self, writer, status, body, headers=None):
        fields = {"Content-Length": str(len(body)), "Connection": "close"}
        fields.update(headers or {})
        writer.write((f"HTTP/1.1 {status} Lab\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n").encode() + body)
        await asyncio.wait_for(writer.drain(), 30)

    async def handle_data(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        source = writer.get_extra_info("peername")[0]
        try:
            label = self.classifier(source)
            if label not in ("clean", "dirty"):
                await self.reply(writer, 403, b"outside_lab")
                return
            while True:
                method, path, headers, body = await self.read_request(reader)
                host = headers.get("host", "").split(":")[0].lower()
                kind = next((k for k, v in self.config["hosts"].items() if v == host), None)
                valid_path = (kind == "login" and path == "/login") or (
                    kind == "manifest" and path == "/manifest") or (
                    kind == "seg" and path.startswith("/seg/") and path[5:].isdigit())
                if method != "GET" or body or not valid_path or self.policy is None:
                    await self.reply(writer, 400, b"invalid_lab_request")
                    return
                run_id, session_id, tunnel_id = (headers.get(key, "") for key in
                    ("x-lab-run-id", "x-lab-session-id", "x-lab-tunnel-id"))
                if not all(TOKEN.fullmatch(v) for v in (run_id, session_id, tunnel_id)):
                    await self.reply(writer, 400, b"invalid_identifiers")
                    return
                self.active += 1
                row = {"event": "request", "run_id": run_id, "session_id": session_id,
                       "tunnel_id": tunnel_id, "class": kind, "source_ip": source,
                       "exit_label": label, "epoch": self.epoch, "cell_id": self.policy["cell_id"],
                       "index": int(path[5:]) if kind == "seg" else 0, "time": time.time(),
                       "body_bytes": 0, "complete": False}
                try:
                    payload = self.verify(headers.get("cookie", ""), run_id, session_id)
                    reason = ""
                    if kind == "login":
                        if label != "clean":
                            reason = "login_denied"
                    elif payload is None:
                        reason = "no_session"
                    elif self.policy["defender"] == "bind" and payload["source"] != source:
                        reason = "bind_mismatch"
                    elif self.policy["defender"] == "every" and label != "clean":
                        reason = "dirty_exit"
                    now = time.monotonic()
                    row["arrival_monotonic_s"] = now
                    start, count = self.counts.get(source, (now, 0))
                    if now - start >= self.policy["window_s"]:
                        start, count = now, 0
                    self.counts[source] = (start, count + 1)
                    if not reason and self.policy["limit"] and count + 1 > self.policy["limit"]:
                        reason = "rate"
                    size = 0 if reason else {"login": 2, "manifest": self.policy["manifest_bytes"],
                                            "seg": self.policy["body_bytes"]}[kind]
                    status = 403 if reason else 200
                    row.update(status=status, reason=reason, declared_bytes=size)
                    extra = ""
                    if kind == "login" and not reason:
                        value = self.sign({"epoch": self.epoch, "run": run_id,
                                           "session": session_id, "source": source})
                        extra = f"Set-Cookie: lab_session={value}; Secure; HttpOnly; Path=/\r\n"
                    writer.write((f"HTTP/1.1 {status} Lab\r\nContent-Length: {size}\r\n"
                                  f"X-Lab-Reason: {reason}\r\n" + extra + "\r\n").encode())
                    remaining = size
                    chunk = b"x" * min(65536, size)
                    while remaining:
                        part = chunk[:remaining]
                        writer.write(part)
                        await asyncio.wait_for(writer.drain(), 30)
                        row["body_bytes"] += len(part)
                        remaining -= len(part)
                    await asyncio.wait_for(writer.drain(), 30)
                    row["complete"] = True
                finally:
                    self.active -= 1
                    self.rows.append(row)
                    self.log(row)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
            self.tasks.discard(task)

    async def handle_admin(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            method, path, headers, body = await self.read_request(reader)
            if not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self.token):
                await self.reply(writer, 403, b"unauthorized")
                return
            if method == "POST" and path == "/policy":
                result = self.set_policy(json.loads(body))
            elif method == "GET" and path.startswith("/rows?cursor="):
                cursor = int(path.split("=", 1)[1])
                if not self.row_base <= cursor <= self.row_base + len(self.rows):
                    raise ValueError("invalid cursor")
                offset = cursor - self.row_base
                rows = self.rows[offset:offset + 2000]
                result = {"rows": rows, "cursor": cursor + len(rows),
                          "total": self.row_base + len(self.rows)}
            elif method == "POST" and path == "/ack":
                cursor = json.loads(body)["cursor"]
                if not isinstance(cursor, int) or not self.row_base <= cursor <= self.row_base + len(self.rows):
                    raise ValueError("invalid acknowledgement")
                del self.rows[:cursor - self.row_base]
                self.row_base = cursor
                result = {"cursor": cursor}
            elif method == "GET" and path == "/health":
                result = {"epoch": self.epoch, "cursor": self.row_base + len(self.rows), "active": self.active,
                          "network": snapshot(), "arrival_clock": "monotonic-v1"}
            else:
                await self.reply(writer, 404, b"unknown_control")
                return
            await self.reply(writer, 200, json.dumps(result).encode(), {"Content-Type": "application/json"})
        except (ValueError, KeyError, TypeError):
            await self.reply(writer, 400, b"invalid_control_request")
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.tasks.discard(task)

    async def close(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve(config):
    log = JsonLog(config["log"])
    origin = Origin(config, log)
    try:
        print(json.dumps(await origin.start()), flush=True)
        stop = asyncio.Event()
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        except NotImplementedError:
            pass
        await stop.wait()
    finally:
        await origin.close()
        log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(serve(json.loads(Path(args.config).read_text(encoding="utf-8"))))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
