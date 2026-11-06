"""Fail-closed Ethernet policy before packets reach Lima's host networking stack."""

from __future__ import annotations

import ctypes
import ipaddress
import socket
import struct
import time

GATEWAY_MAC = bytes.fromhex("5a94efe40cdd")
BROADCAST = b"\xff" * 6
MAX_FRAME = 1514


class Address(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint8),
        ("family", ctypes.c_uint8),
        ("data", ctypes.c_ubyte * 14),
    ]


class Interface(ctypes.Structure):
    pass


Interface._fields_ = [
    ("next", ctypes.POINTER(Interface)),
    ("name", ctypes.c_char_p),
    ("flags", ctypes.c_uint),
    ("address", ctypes.POINTER(Address)),
    ("netmask", ctypes.POINTER(Address)),
    ("destination", ctypes.POINTER(Address)),
    ("data", ctypes.c_void_p),
]
library = ctypes.CDLL(None, use_errno=True)
library.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(Interface))]
library.getifaddrs.restype = ctypes.c_int
library.freeifaddrs.argtypes = [ctypes.POINTER(Interface)]
library.freeifaddrs.restype = None


def host_ipv4() -> set[bytes]:
    """Read every current macOS interface, including public/VPN addresses.

    The egress proxy refuses these, so no name can lead it back to the host itself.
    """
    head = ctypes.POINTER(Interface)()
    if library.getifaddrs(ctypes.byref(head)):
        raise OSError(ctypes.get_errno(), "Cannot enumerate host interface addresses")
    result = set()
    try:
        cursor = head
        while cursor:
            address = cursor.contents.address
            if address and address.contents.family == socket.AF_INET:
                result.add(bytes(address.contents.data[2:6]))
            cursor = cursor.contents.next
    finally:
        library.freeifaddrs(head)
    return result


def ipv4(frame: bytes):
    if not 34 <= len(frame) <= MAX_FRAME or frame[12:14] != b"\x08\x00":
        return None
    ip = frame[14:]
    # No options, fragments, tunnels, VLANs or IPv6. Frame padding is allowed.
    length = int.from_bytes(ip[2:4], "big")
    if ip[0] != 0x45 or ip[6] & 0xBF or ip[7] or not 20 <= length <= len(ip):
        return None
    checksum = sum(struct.unpack("!10H", ip[:20]))
    while checksum >> 16:
        checksum = (checksum & 65535) + (checksum >> 16)
    if checksum != 65535:
        return None
    protocol = ip[9]
    payload = ip[20:length]
    if protocol == 6:
        if len(payload) < 20 or not 20 <= (payload[12] >> 4) * 4 <= len(payload):
            return None
    elif protocol == 17:
        if len(payload) < 8 or int.from_bytes(payload[4:6], "big") != len(payload):
            return None
    else:
        return None
    return ip[12:16], ip[16:20], protocol, payload


class PacketPolicy:
    """Deny by default: the guest may lease its address, answer the host's management
    SSH and, while ``egress`` is on, open TCP to the egress proxy at the usernet
    gateway's ``proxy_port``. Nothing else leaves: no public address, no UDP beyond
    DHCP, no DNS in either protocol.

    Lima's usernet forwards gateway-addressed TCP to the host's loopback at the same
    port, which is where the proxy listens; every other gateway port stays closed.
    """

    def __init__(self, proxy_port, *, egress=True, subnet="192.168.5.0/24"):
        network = ipaddress.ip_network(subnet, strict=True)
        if network.version != 4 or network.prefixlen != 24 or not network.is_private:
            raise ValueError("Packet policy requires a private IPv4 /24 subnet")
        if type(proxy_port) is not int or not 1 <= proxy_port <= 65535:
            raise ValueError("Packet policy requires the egress proxy's port")
        self.network_prefix = network.network_address.packed[:3]
        self.gateway = (network.network_address + 2).packed
        self.proxy_port = proxy_port
        self.mac = None
        self.address = None
        self.ssh_replies = {}
        self.egress = egress

    def outbound(self, frame: bytes) -> bool:
        if not 14 <= len(frame) <= MAX_FRAME:
            return False
        destination_mac, source_mac = frame[:6], frame[6:12]
        if frame[12:14] == b"\x08\x06":
            # ARP is confined to the VM/gateway link and never forwarded to the LAN.
            arp = frame[14:42]
            return bool(
                len(arp) == 28
                and self.mac == source_mac
                and destination_mac in (GATEWAY_MAC, BROADCAST)
                and arp[:6] == b"\x00\x01\x08\x00\x06\x04"
                and arp[6:8] in (b"\x00\x01", b"\x00\x02")
                and arp[8:14] == source_mac
                and arp[14:18] in (self.address, b"\0" * 4)
                and arp[24:28] in (self.gateway, self.address)
            )
        parsed = ipv4(frame)
        if parsed is None:
            return False
        source, destination, protocol, payload = parsed
        sport, dport = struct.unpack("!HH", payload[:4])
        if protocol == 17 and (sport, dport) == (68, 67):
            # Bind the single NIC's identity before accepting its trusted DHCP lease.
            if (
                source not in (b"\0" * 4, self.address)
                or destination not in (b"\xff" * 4, self.gateway)
                or destination_mac not in (BROADCAST, GATEWAY_MAC)
                or len(payload) < 248
                or payload[8:11] != b"\x01\x01\x06"
                or payload[36:42] != source_mac
                or source_mac[0] & 1
            ):
                return False
            if self.mac is None:
                self.mac = source_mac
            return self.mac == source_mac
        if source_mac != self.mac or source != self.address or destination_mac != GATEWAY_MAC:
            return False
        if protocol == 6 and destination == self.gateway and sport == 22:
            # Only replies to host-initiated SSH control connections. A new SYN
            # cannot borrow this grant to dial a host service at the same port.
            last = self.ssh_replies.get(dport)
            if last is not None and time.monotonic() - last < 300 and payload[13] & 0x14:
                self.ssh_replies[dport] = time.monotonic()
                return True
        # The one way out: the proxy, unless the owner has cut egress off.
        return bool(
            self.egress
            and protocol == 6
            and destination == self.gateway
            and dport == self.proxy_port
        )

    def inbound(self, frame: bytes) -> bool:
        if not 14 <= len(frame) <= MAX_FRAME or frame[:6] not in (self.mac, BROADCAST):
            return False
        if frame[12:14] == b"\x08\x06":
            return len(frame) >= 42 and frame[6:12] == GATEWAY_MAC
        parsed = ipv4(frame)
        if parsed is None:
            return False
        source, destination, protocol, payload = parsed
        if frame[6:12] != GATEWAY_MAC:
            return False
        if protocol == 17 and payload[:4] == b"\x00\x43\x00\x44":
            if source != self.gateway or len(payload) < 248 or payload[36:42] != self.mac:
                return False
            assigned = payload[24:28]
            if assigned[:3] != self.network_prefix or assigned[3] not in (1, *range(3, 255)):
                return False
            self.address = assigned
            return True
        if protocol == 6 and source == self.gateway and destination == self.address:
            sport, dport = struct.unpack("!HH", payload[:4])
            if dport == 22:
                now = time.monotonic()
                self.ssh_replies = {
                    port: seen for port, seen in self.ssh_replies.items() if now - seen < 300
                }
                if sport not in self.ssh_replies and len(self.ssh_replies) >= 128:
                    return False
                self.ssh_replies[sport] = now
        return destination == self.address
