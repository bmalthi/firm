import ipaddress
import socket
import struct
import sys

import pytest

from firm_sandbox.packet_policy import BROADCAST, GATEWAY_MAC, PacketPolicy, host_ipv4

MAC = bytes.fromhex("525555abcdef")


def packet(
    source="192.168.5.1",
    destination="8.8.8.8",
    *,
    protocol=6,
    sport=1234,
    dport=443,
    payload=b"",
    source_mac=MAC,
    destination_mac=GATEWAY_MAC,
    fragment=0,
):
    if protocol == 6:
        body = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, 2, 8192, 0, 0) + payload
    else:
        body = struct.pack("!HHHH", sport, dport, len(payload) + 8, 0) + payload
    header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        len(body) + 20,
        0,
        fragment,
        64,
        protocol,
        0,
        ipaddress.IPv4Address(source).packed,
        ipaddress.IPv4Address(destination).packed,
    )
    checksum = sum(struct.unpack("!10H", header))
    while checksum >> 16:
        checksum = (checksum & 65535) + (checksum >> 16)
    header = header[:10] + struct.pack("!H", checksum ^ 65535) + header[12:]
    return destination_mac + source_mac + b"\x08\x00" + header + body


def dhcp(*, response=False, gateway="192.168.5.2", address="192.168.5.1"):
    body = bytearray(240)
    body[:3] = bytes([2 if response else 1, 1, 6])
    body[28:34] = MAC
    if response:
        body[16:20] = ipaddress.IPv4Address(address).packed
    return packet(
        source=gateway if response else "0.0.0.0",
        destination="255.255.255.255",
        protocol=17,
        sport=67 if response else 68,
        dport=68 if response else 67,
        payload=body,
        source_mac=GATEWAY_MAC if response else MAC,
        destination_mac=BROADCAST,
    )


PROXY = {"destination": "192.168.5.2", "dport": 8765}


@pytest.fixture
def policy():
    policy = PacketPolicy(8765)
    assert not policy.outbound(packet(**PROXY))
    assert policy.outbound(dhcp())
    assert policy.inbound(dhcp(response=True))
    return policy


def test_linux_subnet_learns_only_its_own_lease_and_keeps_management_ssh_through_cutoff():
    policy = PacketPolicy(8765, subnet="192.168.94.0/24")
    assert policy.outbound(dhcp())
    assert not policy.inbound(dhcp(response=True))
    assert not policy.inbound(dhcp(response=True, gateway="192.168.94.2"))
    assert policy.inbound(dhcp(
        response=True, gateway="192.168.94.2", address="192.168.94.1"
    ))
    proxy = packet(source="192.168.94.1", destination="192.168.94.2", dport=8765)
    assert policy.outbound(proxy)
    assert not policy.outbound(packet(source="192.168.94.1"))
    assert not policy.outbound(packet(source="192.168.94.1", destination="192.168.94.2"))
    assert not policy.outbound(packet(source="192.168.94.1", destination="192.168.5.2", dport=8765))
    policy.egress = False
    assert not policy.outbound(proxy)
    assert policy.inbound(packet(
        "192.168.94.2", "192.168.94.1", sport=50000, dport=22,
        source_mac=GATEWAY_MAC, destination_mac=MAC,
    ))
    reply = bytearray(packet(
        source="192.168.94.1", destination="192.168.94.2", sport=22, dport=50000
    ))
    reply[47] = 0x10
    assert policy.outbound(bytes(reply))
    assert not policy.outbound(packet(
        source="192.168.94.1", destination="192.168.94.2", sport=22, dport=50001
    ))


@pytest.mark.parametrize("subnet", ["8.8.8.0/24", "192.168.5.0/16", "::/24"])
def test_packet_policy_rejects_unsupported_subnet(subnet):
    with pytest.raises(ValueError):
        PacketPolicy(8765, subnet=subnet)


@pytest.mark.parametrize("port", [None, 0, 65536, "8765", True])
def test_packet_policy_requires_the_proxy_port(port):
    with pytest.raises(ValueError, match="proxy"):
        PacketPolicy(port)


@pytest.mark.parametrize(
    "destination",
    [
        "127.0.0.1",
        "192.168.5.2",
        "10.1.2.3",
        "192.168.1.1",
        "172.16.1.1",
        "169.254.169.254",
        "100.100.100.200",
        "0.0.0.0",
        "224.0.0.1",
        "9.9.9.9",
        "8.8.8.8",
        "1.1.1.1",
    ],
)
def test_host_lan_metadata_and_public_addresses_are_blocked(policy, destination):
    assert not policy.outbound(packet(destination=destination))
    if destination != PROXY["destination"]:
        assert not policy.outbound(packet(destination=destination, dport=PROXY["dport"]))


def test_only_tcp_to_the_proxy_leaves(policy):
    assert policy.outbound(packet(**PROXY))
    assert not policy.outbound(packet(destination="192.168.5.2", dport=8764))
    assert not policy.outbound(packet(destination="192.168.5.2", dport=22))
    assert not policy.outbound(packet(destination="192.168.5.2", protocol=17, dport=8765))
    assert not policy.outbound(packet(destination="8.8.8.8", dport=8765))


@pytest.mark.parametrize("destination", ["8.8.8.8", "192.168.5.2", "1.1.1.1"])
@pytest.mark.parametrize("protocol", [6, 17])
def test_dns_is_dropped_in_both_protocols(policy, destination, protocol):
    assert not policy.outbound(
        packet(destination=destination, protocol=protocol, dport=53, payload=b"dns")
    )


@pytest.mark.parametrize("dport", [123, 443, 8765, 51820])
def test_udp_beyond_dhcp_is_dropped(policy, dport):
    assert not policy.outbound(packet(protocol=17, dport=dport, payload=b"datagram"))
    assert not policy.outbound(
        packet(destination="192.168.5.2", protocol=17, dport=dport, payload=b"datagram")
    )


def test_spoofing_and_fragmentation_are_rejected(policy):
    for arguments in [
        {"source": "192.168.5.9"},
        {"source_mac": b"\x02" * 6},
        {"destination_mac": b"\x02" * 6},
        {"fragment": 0x2000},
        {"fragment": 1},
        {"protocol": 1},
        {"protocol": 41},
    ]:
        assert not policy.outbound(packet(**{**PROXY, **arguments}))
    assert policy.outbound(packet(**PROXY, fragment=0x4000))


def test_malformed_and_alternative_frames_are_rejected(policy):
    original = packet(**PROXY)
    for frame in [
        b"",
        original[:30],
        original + bytes(1600),
        original[:12] + b"\x86\xdd" + original[14:],
        original[:12] + b"\x81\x00" + original[14:],
        original[:14] + b"\x46" + original[15:],
        original[:24] + b"\x00\x00" + original[26:],
    ]:
        assert not policy.outbound(frame)


def test_inbound_frames_must_target_the_bound_nic(policy):
    assert policy.inbound(
        packet("8.8.8.8", "192.168.5.1", source_mac=GATEWAY_MAC, destination_mac=MAC)
    )
    assert not policy.inbound(
        packet("8.8.8.8", "192.168.5.9", source_mac=GATEWAY_MAC, destination_mac=MAC)
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS interface enumeration")
def test_native_host_addresses_include_loopback():
    assert socket.inet_aton("127.0.0.1") in host_ipv4()


@pytest.mark.parametrize("now", [0.0, 100.0, 1000.0])
def test_control_ssh_grant_allows_only_replies_to_observed_host_connections(
    policy, monkeypatch, now
):
    monkeypatch.setattr("firm_sandbox.packet_policy.time.monotonic", lambda: now)
    outbound = packet(destination="192.168.5.2", sport=22, dport=50000)
    acknowledged = outbound[:47] + bytes([0x12]) + outbound[48:]
    assert not policy.outbound(outbound)
    assert not policy.outbound(acknowledged)
    assert policy.inbound(
        packet(
            "192.168.5.2",
            "192.168.5.1",
            sport=50000,
            dport=22,
            source_mac=GATEWAY_MAC,
            destination_mac=MAC,
        )
    )
    assert policy.outbound(acknowledged)
    assert not policy.outbound(outbound)
    assert not policy.outbound(packet(destination="192.168.5.2", sport=22, dport=50001))


def test_cutoff_blocks_the_proxy_but_keeps_control(policy):
    policy.egress = False
    assert not policy.outbound(packet(**PROXY))
    assert policy.inbound(packet(
        "192.168.5.2", "192.168.5.1", sport=50000, dport=22,
        source_mac=GATEWAY_MAC, destination_mac=MAC,
    ))
    reply = packet(destination="192.168.5.2", sport=22, dport=50000)
    reply = reply[:47] + bytes([0x12]) + reply[48:]
    assert policy.outbound(reply)
    policy.egress = True
    assert policy.outbound(packet(**PROXY))


def test_traffic_metadata_is_bounded_and_contains_no_payload():
    from firm_sandbox.network_observation import Traffic

    traffic = Traffic(capacity=1)
    first = packet(destination="8.8.8.8", payload=b"private-content")
    traffic.observe(first, True)
    traffic.observe(first, False)
    traffic.observe(packet(destination="1.1.1.1"), True)
    result = traffic.snapshot()
    assert len(result["destinations"]) == 1
    assert result["destinations"][0]["allowed_bytes"] == len(first)
    assert result["blocked_bytes"] == len(first)
    assert result["overflow_bytes"] > 0
    assert "private-content" not in str(result)
