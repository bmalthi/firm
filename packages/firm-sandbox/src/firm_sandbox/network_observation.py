"""Bounded destination metadata; never inspect or retain application payloads.

With egress closed, the blocked bytes here are frames that tried to go around the proxy.
"""

import ipaddress
import struct
import threading
import time

from .packet_policy import ipv4


class Traffic:
    def __init__(self, *, capacity=256, gateway="192.168.5.2"):
        self.capacity = capacity
        self.gateway = ipaddress.IPv4Address(gateway).packed
        self.destinations = {}
        self.allowed_bytes = self.blocked_bytes = self.overflow_bytes = 0
        self.started = time.time()
        self.lock = threading.Lock()

    def observe(self, frame, allowed):
        packet = ipv4(frame)
        if packet is None:
            return
        _, destination, protocol, payload = packet
        source_port, port = struct.unpack("!HH", payload[:4])
        if destination == self.gateway and source_port == 22:
            return  # The runtime's management SSH channel is not egress.
        key = (str(ipaddress.IPv4Address(destination)), port, "tcp" if protocol == 6 else "udp")
        now = time.time()
        with self.lock:
            if allowed:
                self.allowed_bytes += len(frame)
            else:
                self.blocked_bytes += len(frame)
            if key not in self.destinations:
                if len(self.destinations) >= self.capacity:
                    self.overflow_bytes += len(frame)
                    return
                self.destinations[key] = {
                    "address": key[0],
                    "port": port,
                    "protocol": key[2],
                    "first_seen": now,
                    "last_seen": now,
                    "allowed_bytes": 0,
                    "blocked_bytes": 0,
                }
            record = self.destinations[key]
            record["last_seen"] = now
            record["allowed_bytes" if allowed else "blocked_bytes"] += len(frame)

    def snapshot(self):
        with self.lock:
            return {
                "observed_at": time.time(),
                "since": self.started,
                "allowed_bytes": self.allowed_bytes,
                "blocked_bytes": self.blocked_bytes,
                "overflow_bytes": self.overflow_bytes,
                "units": "outbound Ethernet bytes including headers and retransmissions",
                "destinations": [dict(item) for item in self.destinations.values()],
            }
