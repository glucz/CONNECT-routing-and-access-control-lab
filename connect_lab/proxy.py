"""Run with python -m connect_lab.proxy --config pool.json."""
import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import socket
import signal
import time
from pathlib import Path


SETS = {"clean-stick": ("clean", "stick"), "clean-rot": ("clean", "rot"),
        "dirty-stick": ("dirty", "stick")}
TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,160}\Z")


class JsonLog:
    def __init__(self, path):
        # Never silently append a restarted allocator/budget to a prior experiment.
        self.file = Path(path).open("x", encoding="utf-8")

    def __call__(self, event):
        self.file.write(json.dumps(event, sort_keys=True) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


class AddressPool:
    def __init__(self, prefix):
        self.network = ipaddress.IPv6Network(prefix)
        if self.network.prefixlen != 64:
            raise ValueError("exit_prefix must be an IPv6 /64")
        self.counters = {}
        self.owners = {}

    def allocate(self, exit_set, identity):
        label, mode = SETS[exit_set]
        key = (exit_set, identity)
        counter = self.counters.get(key, 0) + 1 if mode == "rot" else 0
        self.counters[key] = counter
        owner = (exit_set, identity, counter)
        digest = hashlib.sha256(json.dumps(owner).encode()).digest()
        # Exactly 64 host bits: 16 class bits, 1 mode bit, 47 hash bits.
        host = ((0xC if label == "clean" else 0xD) << 48)
        host |= (mode == "rot") << 47
        host |= int.from_bytes(digest[:6], "big") & ((1 << 47) - 1)
        address = str(ipaddress.IPv6Address(int(self.network.network_address) | host))
        if address in self.owners and self.owners[address] != owner:
            raise RuntimeError("address collision; stop and invalidate this run")
        self.owners[address] = owner
        return address


class ExitProxy:
    def __init__(self, config, log, allocator=None):
        self.config, self.log = config, log
        self.pool = allocator or AddressPool(config["exit_prefix"])
        self.budget = int(config["byte_budget"])
        if self.budget <= 0:
            raise ValueError("byte_budget must be positive")
        self.used = 0
        self.exhausted = False
        self.servers, self.writers, self.tasks = [], set(), set()
        self.tunnel_ids = set()
        self.targets = {}
        for hostname, target in config["targets"].items():
            if not re.fullmatch(r"[a-z0-9.-]+", hostname):
                raise ValueError("targets must use lowercase DNS hostnames")
            self.targets[(hostname, int(target["port"]))] = str(ipaddress.ip_address(target["ip"]))
        if not self.targets:
            raise ValueError("at least one pinned lab target is required")
        self.timeout = float(config.get("idle_timeout_s", 35))

    async def start(self):
        try:
            for exit_set, port in self.config["listeners"].items():
                if exit_set not in SETS:
                    raise ValueError("unknown exit set")
                server = await asyncio.start_server(
                    lambda r, w, s=exit_set: self.handle(r, w, s),
                    "127.0.0.1", int(port), limit=16384)
                self.servers.append(server)
            return {name: server.sockets[0].getsockname()[1]
                    for name, server in zip(self.config["listeners"], self.servers)}
        except BaseException:
            await self.close()
            raise

    def stop_budget(self):
        if not self.exhausted:
            self.exhausted = True
            self.log({"event": "budget_exhausted", "bytes": self.used, "time": time.time()})
        for writer in list(self.writers):
            writer.close()

    async def relay(self, reader, writer, event, direction):
        while True:
            data = await asyncio.wait_for(reader.read(65536), self.timeout)
            if not data:
                if writer.can_write_eof():
                    writer.write_eof()
                return
            remaining = self.budget - self.used
            chunk = data[:remaining]
            # No await between reserving budget and enqueueing: shared across all exits.
            self.used += len(chunk)
            event[direction] += len(chunk)
            writer.write(chunk)
            await asyncio.wait_for(writer.drain(), self.timeout)
            if self.used >= self.budget:
                self.stop_budget()
                return

    async def handle(self, reader, writer, exit_set):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        remote = None
        pumps = []
        established = False
        event = {"event": "tunnel", "exit_set": exit_set, "class": SETS[exit_set][0],
                 "start": time.time(), "bytes_up": 0, "bytes_down": 0}
        try:
            if self.exhausted:
                raise ValueError("budget_exhausted")
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = raw.decode("ascii").split("\r\n")
            method, authority, version = lines[0].split()
            if method != "CONNECT" or version not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("CONNECT_required")
            hostname, port_text = authority.rsplit(":", 1)
            target = (hostname.lower(), int(port_text))
            if target not in self.targets:
                raise ValueError("destination_denied")
            headers = {}
            for line in lines[1:-2]:
                name, value = line.split(":", 1)
                name = name.lower()
                if name in headers:
                    raise ValueError("duplicate_header")
                headers[name] = value.strip()
            if "transfer-encoding" in headers or headers.get("content-length", "0") != "0":
                raise ValueError("CONNECT_body_denied")
            scheme, encoded = headers.get("proxy-authorization", "").split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError("identity_required")
            identity, _ = base64.b64decode(encoded, validate=True).decode("ascii").split(":", 1)
            tunnel_id = headers.get("x-lab-tunnel-id", "")
            if not TOKEN.fullmatch(identity) or not TOKEN.fullmatch(tunnel_id):
                raise ValueError("invalid_identity_or_tunnel_id")
            if tunnel_id in self.tunnel_ids:
                raise ValueError("duplicate_tunnel_id")
            self.tunnel_ids.add(tunnel_id)
            source = self.pool.allocate(exit_set, identity)
            event.update(identity=identity, tunnel_id=tunnel_id, source_ip=source,
                         target=authority)
            family = socket.AF_INET6 if ":" in source else socket.AF_INET
            upstream, remote = await asyncio.wait_for(asyncio.open_connection(
                self.targets[target], target[1], family=family, local_addr=(source, 0)), 10)
            self.writers.add(remote)
            event["bound_source_ip"] = remote.get_extra_info("sockname")[0]
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            established = True
            pumps = [asyncio.create_task(self.relay(reader, remote, event, "bytes_up")),
                     asyncio.create_task(self.relay(upstream, writer, event, "bytes_down"))]
            await asyncio.gather(*pumps)
            event["outcome"] = "budget_exhausted" if self.exhausted else "closed"
        except asyncio.CancelledError:
            event["outcome"] = "shutdown"
            raise
        except Exception as exc:
            event["outcome"] = "budget_exhausted" if self.exhausted else type(exc).__name__
            if isinstance(exc, OSError):
                event["errno"] = exc.errno
                event["os_error"] = exc.strerror
            if isinstance(exc, ValueError):
                event["reason"] = str(exc)[:120]
            if not established and not writer.is_closing():
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                try:
                    await writer.drain()
                except OSError:
                    pass
        finally:
            for pump in pumps:
                pump.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (writer, remote):
                if stream is not None:
                    self.writers.discard(stream)
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except OSError:
                        pass
            event["end"] = time.time()
            self.log(event)
            self.tasks.discard(task)

    async def close(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve(config):
    log = JsonLog(config["log"])
    pool = ExitProxy(config, log)
    try:
        ports = await pool.start()
        log({"event": "start", "time": time.time(), "config": config, "ports": ports})
        print(json.dumps({"listening": ports}), flush=True)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, stopped.set)
        except NotImplementedError:  # Windows; Ctrl+C still uses asyncio.run's cleanup.
            pass
        await stopped.wait()
    finally:
        await pool.close()
        log({"event": "stop", "time": time.time(), "bytes": pool.used,
             "budget_exhausted": pool.exhausted})
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
