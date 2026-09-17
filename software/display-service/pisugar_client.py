"""Minimal client for pisugar-server's line-based TCP protocol (127.0.0.1:8423).

pisugar-server (installed via the official pisugar-power-manager.sh script)
exposes battery/charging/model info over a plain-text request/response
protocol on this port, plus a web UI on :8421. No official Python client is
needed for simple reads - this is a thin wrapper around raw socket I/O.
"""
import socket

HOST, PORT = "127.0.0.1", 8423


def _query(*commands, timeout=2.0):
    with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
        sock.sendall(("\n".join(commands) + "\n").encode())
        sock.settimeout(timeout)
        chunks = []
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except socket.timeout:
            pass
    return b"".join(chunks).decode(errors="replace")


def get_status():
    """Returns a dict with battery percent (int), charging (bool), voltage (float)."""
    raw = _query("get battery", "get battery_charging", "get battery_v")
    out = {}
    for line in raw.strip().splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
    return {
        "percent": int(float(out.get("battery", 0))),
        "charging": out.get("battery_charging") == "true",
        "voltage": float(out.get("battery_v", 0)),
    }


if __name__ == "__main__":
    print(get_status())
