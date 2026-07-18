import importlib.util
import socket
import struct
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("socket_cleaner", ROOT / "remnanode-xhttp-clean.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def record(*, idle_ms=300_000, inode=42, cookie=(1, 2), local_port=40000, remote="8.8.8.8"):
    return MODULE.SocketRecord(
        family=socket.AF_INET,
        state=MODULE.TCP_ESTABLISHED,
        local_address="192.0.2.10",
        local_port=local_port,
        remote_address=remote,
        remote_port=443,
        interface=0,
        cookie=cookie,
        inode=inode,
        recv_queue=0,
        send_queue=0,
        last_sent_ms=idle_ms,
        last_received_ms=idle_ms,
        sockid=b"x" * 48,
    )


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.config = MODULE.Config(idle_seconds=300)

    def reason(self, item, listen_ports=None):
        return MODULE.candidate_reason(item, {42}, listen_ports or set(), self.config)

    def test_exactly_five_minutes_is_stale(self):
        self.assertIsNotNone(self.reason(record(idle_ms=300_000)))

    def test_less_than_five_minutes_is_active(self):
        self.assertIsNone(self.reason(record(idle_ms=299_999)))

    def test_activity_in_either_direction_keeps_socket(self):
        item = record(idle_ms=600_000)
        item = MODULE.dataclasses.replace(item, last_received_ms=1_000)
        self.assertIsNone(self.reason(item))

    def test_socket_must_be_owned_by_xray(self):
        self.assertIsNone(MODULE.candidate_reason(record(inode=99), {42}, set(), self.config))

    def test_inbound_listening_port_is_excluded(self):
        self.assertIsNone(self.reason(record(local_port=443), {443}))

    def test_loopback_is_excluded(self):
        self.assertIsNone(self.reason(record(remote="127.0.0.1")))

    def test_idle_floor_cannot_be_lowered(self):
        with self.assertRaises(MODULE.CleanerError):
            MODULE.Config(idle_seconds=299).validate()

    def test_cookie_distinguishes_reused_tuple(self):
        old = record(cookie=(1, 2))
        new = record(cookie=(3, 4))
        self.assertNotEqual(old.identity, new.identity)


class WireFormatTests(unittest.TestCase):
    def test_destroy_request_keeps_cookie(self):
        cookie = (0x11223344, 0x55667788)
        sockid = b"\0" * 40 + struct.pack("=II", *cookie)
        payload = MODULE.DiagClient._request_payload(
            socket.AF_INET, 0, sockid, request_info=False
        )
        self.assertEqual(struct.unpack_from("=II", payload, 48), cookie)


if __name__ == "__main__":
    unittest.main()
