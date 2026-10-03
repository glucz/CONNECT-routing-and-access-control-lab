"""Run one lab job: python -m connect_lab.driver --config job.json --output NEW_DIR."""
import argparse
import base64
import hashlib
import http.client
from http.cookies import SimpleCookie
import json
import math
from pathlib import Path
import ssl
import socket
import threading
import time
import uuid

from .proxy import JsonLog, TOKEN


ROUTES = {
    "R-dirty-stick": ("dirty-stick", "dirty-stick", "dirty-stick"),
    "R-clean-rot": ("clean-rot", "clean-rot", "clean-rot"),
    "R-split": ("clean-stick", "clean-stick", "dirty-stick"),
    "R-clean-stick": ("clean-stick", "clean-stick", "clean-stick"),
}


class TunnelHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)
        # Assign the SSL socket before its blocking handshake so the deadline can abort it.
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._tunnel_host or self.host,
            do_handshake_on_connect=False)
        self.sock.do_handshake()


def validate(config):
    if config["routing"] not in ROUTES or config["workload"] not in ("video", "scrape"):
        raise ValueError("unknown routing policy or workload")
    for key in ("count", "body_bytes", "reuse", "sessions"):
        if not isinstance(config[key], int) or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if config["sessions"] > config["count"]:
        raise ValueError("cannot have more sessions than records")
    if config["workload"] == "video" and config["sessions"] != 1:
        raise ValueError("video uses exactly one session")
    if not 0 < config.get("timeout_s", 30) <= 300:
        raise ValueError("invalid timeout_s")
    interval = config.get("request_interval_s", 0)
    if not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 <= interval <= 60:
        raise ValueError("invalid request_interval_s")
    reconnect = config.get("reconnect_every", 0)
    if not isinstance(reconnect, int) or reconnect < 0:
        raise ValueError("invalid reconnect_every")
    if config.get("reconnect_source", "hold") not in ("hold", "rotate"):
        raise ValueError("invalid reconnect_source")
    for kind in ("login", "manifest", "seg"):
        endpoint = config["targets"][kind]
        if any(c in endpoint["host"] for c in "/:@\r\n"):
            raise ValueError("target host must be a DNS hostname")
        path = endpoint["path"].format(index=0)
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("invalid request path")
    for exit_set in set(ROUTES[config["routing"]]):
        if exit_set not in config["proxies"]:
            raise ValueError(f"missing {exit_set} port")


class Session:
    def __init__(self, config, context, identity, run_id, log):
        self.config, self.context, self.identity = config, context, identity
        self.run_id, self.log = run_id, log
        self.connections = {}
        self.cookie = None
        self.serial = 0
        self.generation = 0
        self.last_start = None

    def close(self):
        for connection, _, _ in self.connections.values():
            connection.close()
        self.connections.clear()

    def request(self, kind, index=0, expected_bytes=None, forced_reconnect=False):
        interval = self.config.get("request_interval_s", 0)
        if self.last_start is not None:
            time.sleep(max(0, self.last_start + interval - time.monotonic()))
        started = time.monotonic()
        self.last_start = started
        timeout = self.config.get("timeout_s", 30)
        deadline = started + timeout
        target = self.config["targets"][kind]
        exit_set = ROUTES[self.config["routing"]][("login", "manifest", "seg").index(kind)]
        key = (exit_set, target["host"], target.get("port", 443))
        event = {"run_id": self.run_id, "session_id": self.identity, "class": kind,
                 "index": index, "exit_set": exit_set, "time": time.time(),
                 "status": None, "body_bytes": 0, "complete": False, "ok": False}
        event["source_generation"] = self.generation
        event["forced_reconnect"] = forced_reconnect
        conn = None
        def expire():
            # Enforce a whole-request deadline, including a trickling response header/body.
            if conn is not None and conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        watchdog = threading.Timer(timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            entry = self.connections.get(key)
            if entry is None or entry[1] >= self.config["reuse"] or entry[0].sock is None:
                if entry:
                    entry[0].close()
                self.serial += 1
                tunnel_id = f"{self.identity}.t{self.serial}"
                conn = TunnelHTTPSConnection("127.0.0.1", self.config["proxies"][exit_set],
                                             timeout=timeout, context=self.context)
                proxy_identity = self.identity + (f".r{self.generation}" if self.generation else "")
                auth = base64.b64encode((proxy_identity + ":lab").encode()).decode()
                conn.set_tunnel(target["host"], target.get("port", 443), headers={
                    "Proxy-Authorization": "Basic " + auth,
                    "X-Lab-Tunnel-Id": tunnel_id,
                })
                self.connections[key] = (conn, 0, tunnel_id)
                event["tunnel_id"] = tunnel_id
                conn.connect()
            conn, used, tunnel_id = self.connections[key]
            event["tunnel_id"] = tunnel_id
            self.connections[key] = (conn, used + 1, tunnel_id)
            headers = {"X-Lab-Run-Id": self.run_id, "X-Lab-Session-Id": self.identity,
                       "X-Lab-Tunnel-Id": tunnel_id, "Accept-Encoding": "identity"}
            if self.cookie is not None:
                # Explicit sharing is confined to the three configured lab hosts.
                headers["Cookie"] = self.cookie
            conn.sock.settimeout(max(0.001, deadline - time.monotonic()))
            response_socket = conn.sock
            conn.request("GET", target["path"].format(index=index), headers=headers)
            response = conn.getresponse()
            event["status"] = response.status
            event["reason"] = response.getheader("X-Lab-Reason", "")
            content_length = response.getheader("Content-Length")
            # Missing framing must not turn a truncated close into a successful delivery.
            if content_length is None or not content_length.isdigit():
                raise ValueError("valid_Content-Length_required")
            declared = int(content_length)
            received_cookie = None
            if kind == "login" and response.status == 200:
                cookies = SimpleCookie()
                for name, value in response.getheaders():
                    if name.lower() == "set-cookie":
                        cookies.load(value)
                cookie_name = self.config.get("cookie_name", "lab_session")
                if cookie_name not in cookies:
                    raise ValueError("session_cookie_missing")
                received_cookie = cookies[cookie_name].OutputString(attrs=[])
            # Hold the socket reference even if getresponse detaches a close-delimited response.
            while event["body_bytes"] < declared:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise TimeoutError("request_deadline")
                if response_socket is not None:
                    response_socket.settimeout(remaining_time)
                chunk = response.read1(min(65536, declared - event["body_bytes"]))
                if not chunk:
                    raise EOFError("truncated_body")
                event["body_bytes"] += len(chunk)
            response.close()
            if time.monotonic() > deadline:
                raise TimeoutError("request_deadline")
            event["complete"] = True
            event["ok"] = response.status == 200 and (
                expected_bytes is None or event["body_bytes"] == expected_bytes)
            if response.status == 200 and not event["ok"]:
                event["reason"] = "body_size_mismatch"
                event["error"] = "BodySizeMismatch"
            if response.status not in (200, 403):
                event["error"] = "UnexpectedHTTPStatus"
            if event["ok"] and received_cookie:
                self.cookie = received_cookie
        except Exception as exc:
            event["error"] = type(exc).__name__
            if conn:
                conn.close()
        finally:
            watchdog.cancel()
            watchdog.join()
        event["duration_s"] = time.monotonic() - started
        self.log(event)
        return event


def run_job(config, log):
    validate(config)
    run_id = config.get("run_id", uuid.uuid4().hex)
    if not TOKEN.fullmatch(run_id):
        raise ValueError("invalid run_id")
    context = ssl.create_default_context(cafile=config["ca_file"])
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["http/1.1"])
    started = time.monotonic()
    stats = {"run_id": run_id, "workload": config["workload"], "routing": config["routing"],
             "requested": config["count"], "sessions": config["sessions"],
             "n_requests": 0, "n_ok": 0, "n_denied": 0, "records_ok": 0,
             "body_bytes": 0, "transport_errors": 0, "first_failure": None}

    def record(event):
        stats["n_requests"] += 1
        stats["n_ok"] += event["ok"]
        stats["n_denied"] += event["status"] == 403
        stats["body_bytes"] += event["body_bytes"]
        stats["transport_errors"] += "error" in event
        if not event["ok"] and stats["first_failure"] is None:
            stats["first_failure"] = event.get("error") or event.get("reason") or str(event["status"])
        log(event)

    offset = 0
    for number in range(config["sessions"]):
        count = config["count"] // config["sessions"] + (number < config["count"] % config["sessions"])
        # Short and deterministic from the unique run identifier, with a separate session suffix.
        identity = hashlib.sha256(run_id.encode()).hexdigest()[:32] + f".s{number}"
        session = Session(config, context, identity, run_id, record)
        try:
            login = session.request("login")
            if config["workload"] == "video":
                if not login["ok"]:
                    break
                manifest = session.request("manifest", expected_bytes=config.get("manifest_bytes", 10000))
                if not manifest["ok"]:
                    break
            for index in range(offset, offset + count):
                every = config.get("reconnect_every", 0)
                forced = bool(every and index > offset and (index - offset) % every == 0)
                if forced:
                    session.close()  # Preserve the authenticated application session and cookie.
                    if config.get("reconnect_source", "hold") == "rotate":
                        session.generation += 1
                event = session.request("seg", index, config["body_bytes"], forced_reconnect=forced)
                stats["records_ok"] += event["ok"]
                if not event["ok"] and config["workload"] == "video":
                    break
        finally:
            session.close()
        offset += count
    minimum = config["count"] if config["workload"] == "video" else math.ceil(.99 * config["count"])
    stats["success"] = stats["records_ok"] >= minimum
    stats["delivery_fraction"] = stats["records_ok"] / config["count"]
    stats["complete_delivery"] = stats["records_ok"] == config["count"]
    stats["success_99"] = stats["records_ok"] >= (99*config["count"] + 99)//100
    stats["valid"] = stats["transport_errors"] == 0
    stats["duration_s"] = time.monotonic() - started
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    validate(config)
    config.setdefault("run_id", uuid.uuid4().hex)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    log = JsonLog(output / "requests.jsonl")
    try:
        result = run_job(config, log)
        (output / "run.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        log.close()


if __name__ == "__main__":
    main()
