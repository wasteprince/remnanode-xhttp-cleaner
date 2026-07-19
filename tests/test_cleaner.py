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


def record(
    *,
    idle_ms=300_000,
    inode=42,
    cookie=(1, 2),
    local="192.0.2.10",
    local_port=40000,
    remote="8.8.8.8",
):
    return MODULE.SocketRecord(
        family=socket.AF_INET,
        state=MODULE.TCP_ESTABLISHED,
        local_address=local,
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
        self.config = MODULE.Config(
            idle_seconds=300,
            clean_xhttp_buffers=True,
            clean_established_outbound=True,
        )

    def reason(self, item, listen_ports=None):
        return MODULE.candidate_reason(item, {42}, listen_ports or set(), self.config)

    def test_exactly_five_minutes_is_stale(self):
        self.assertIsNotNone(self.reason(record(idle_ms=300_000)))

    def test_established_outbound_is_protected_by_default_for_tcp_bridges(self):
        config = MODULE.Config(idle_seconds=300)
        self.assertIsNone(
            MODULE.candidate_kind(record(idle_ms=3_600_000), {42}, set(), config)
        )

    def test_close_wait_is_reaped_without_enabling_established_cleanup(self):
        item = MODULE.dataclasses.replace(record(), state=MODULE.TCP_CLOSE_WAIT)
        config = MODULE.Config(idle_seconds=300)
        self.assertEqual(
            MODULE.candidate_kind(item, {42}, set(), config), "CLOSE-WAIT"
        )

    def test_close_wait_cleanup_can_be_disabled(self):
        item = MODULE.dataclasses.replace(record(), state=MODULE.TCP_CLOSE_WAIT)
        config = MODULE.Config(idle_seconds=300, clean_close_wait=False)
        self.assertIsNone(MODULE.candidate_kind(item, {42}, set(), config))

    def test_default_diagnostic_dump_does_not_request_established_sockets(self):
        self.assertEqual(
            MODULE.diagnostic_states(MODULE.Config()), (MODULE.TCP_CLOSE_WAIT,)
        )

    def test_established_dump_is_opt_in(self):
        states = MODULE.diagnostic_states(
            MODULE.Config(clean_established_outbound=True)
        )
        self.assertIn(MODULE.TCP_ESTABLISHED, states)
        self.assertIn(MODULE.TCP_LISTEN, states)

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

    def test_idle_xhttp_inbound_is_included(self):
        item = record(local="127.0.0.1", local_port=8443, remote="127.0.0.1")
        listeners = [MODULE.XhttpListener(address="127.0.0.1", port=8443)]
        self.assertEqual(
            MODULE.candidate_kind(item, {42}, {8443}, self.config, listeners),
            "XHTTP-BUFFER",
        )

    def test_active_xhttp_inbound_is_excluded(self):
        item = record(idle_ms=299_999, local_port=8443)
        listeners = [MODULE.XhttpListener(address="0.0.0.0", port=8443)]
        self.assertIsNone(
            MODULE.candidate_kind(item, {42}, {8443}, self.config, listeners)
        )

    def test_xhttp_cleanup_can_be_disabled(self):
        item = record(local_port=8443)
        listeners = [MODULE.XhttpListener(address="0.0.0.0", port=8443)]
        config = MODULE.Config(clean_xhttp_buffers=False)
        self.assertIsNone(MODULE.candidate_kind(item, {42}, {8443}, config, listeners))

    def test_xhttp_listener_address_must_match(self):
        item = record(local="192.0.2.10", local_port=8443)
        listeners = [MODULE.XhttpListener(address="127.0.0.1", port=8443)]
        self.assertIsNone(
            MODULE.candidate_kind(item, {42}, {8443}, self.config, listeners)
        )

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


class ProcessMetricTests(unittest.TestCase):
    def test_proc_stat_parser_handles_spaces_and_parentheses_in_name(self):
        # Fields after comm begin with state (field 3); utime/stime are 14/15.
        tail = ["S"] + ["0"] * 10 + ["120", "30"] + ["0"] * 8
        line = "42 (rw core (worker)) " + " ".join(tail)
        self.assertEqual(MODULE.process_cpu_ticks(line), 150)


class XhttpConfigTests(unittest.TestCase):
    def test_extracts_xhttp_and_legacy_splithttp_only(self):
        listeners = MODULE.parse_xhttp_listeners(
            {
                "inbounds": [
                    {
                        "tag": "xhttp-main",
                        "listen": "127.0.0.1",
                        "port": 8443,
                        "streamSettings": {"network": "xhttp"},
                    },
                    {
                        "tag": "legacy",
                        "port": "9443",
                        "streamSettings": {"network": "splithttp"},
                    },
                    {
                        "tag": "ws",
                        "port": 7443,
                        "streamSettings": {"network": "ws"},
                    },
                ]
            }
        )
        self.assertEqual(
            listeners,
            [
                MODULE.XhttpListener("127.0.0.1", 8443, "xhttp-main"),
                MODULE.XhttpListener("", 9443, "legacy"),
            ],
        )

    def test_unix_listener_is_ignored(self):
        listeners = MODULE.parse_xhttp_listeners(
            {
                "inbounds": [
                    {
                        "listen": "/run/xhttp.sock",
                        "port": 443,
                        "streamSettings": {"network": "xhttp"},
                    }
                ]
            }
        )
        self.assertEqual(listeners, [])

    def test_counts_xhttp_tcp_and_grpc_in_both_directions(self):
        counts = MODULE.parse_transport_counts(
            {
                "inbounds": [
                    {"streamSettings": {"network": "xhttp"}},
                    {"streamSettings": {"network": "grpc"}},
                ],
                "outbounds": [
                    {"streamSettings": {"network": "raw"}},
                    {"streamSettings": {"network": "splithttp"}},
                ],
            }
        )
        self.assertEqual(counts, {"xhttp": 2, "tcp": 1, "grpc": 1})


if __name__ == "__main__":
    unittest.main()
