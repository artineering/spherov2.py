from spherov2.controls import CommandExecuteError, PacketDecodingException
from spherov2.controls.v2 import Packet
import pytest


def test_build_escapes_reserved_bytes_and_roundtrips():
    m = Packet.Manager()
    p = m.new_packet(0x16, 0x07, None, [Packet.Encoding.escape, Packet.Encoding.start, Packet.Encoding.end, 1])
    raw = p.build()
    assert raw[0] == Packet.Encoding.start and raw[-1] == Packet.Encoding.end
    # reserved bytes never appear raw inside the frame
    assert Packet.Encoding.start not in raw[1:-1] and Packet.Encoding.end not in raw[1:-1]
    back = Packet.parse_response(list(raw))
    assert back.did == 0x16 and back.cid == 0x07 and back.seq == p.seq
    assert bytes(back.data) == bytes([0xAB, 0x8D, 0xD8, 1])


def test_target_and_source_ids_encoded_when_proc_given():
    m = Packet.Manager()
    p = m.new_packet(0x18, 0x01, 0x12, [])
    assert p.flags & Packet.Flags.has_target_id and p.flags & Packet.Flags.has_source_id
    back = Packet.parse_response(list(p.build()))
    assert back.tid == 0x12 and back.sid == 0x01


def test_sequence_uses_full_byte_range():
    m = Packet.Manager()
    seqs = {m.new_packet(1, 1).seq for _ in range(512)}
    assert seqs == set(range(256))


def test_bad_checksum_rejected():
    raw = bytearray(Packet.Manager().new_packet(1, 2, None, [5]).build())
    raw[-2] ^= 0xff
    with pytest.raises(PacketDecodingException):
        Packet.parse_response(list(raw))


def test_error_response_raises_on_check():
    req = Packet.Manager().new_packet(1, 2)
    resp = Packet(req.flags | Packet.Flags.is_response, 1, 2, req.seq, None, None, bytearray(), Packet.Error.busy)
    back = Packet.parse_response(list(resp.build()))
    assert back.err == Packet.Error.busy
    with pytest.raises(CommandExecuteError):
        back.check_error()


class TestCollector:
    def _collect(self):
        got = []
        return got, Packet.Collector(got.append)

    def test_single_notification(self):
        got, c = self._collect()
        c.add(Packet.Manager().new_packet(1, 2, None, [9]).build())
        assert len(got) == 1 and got[0].data == bytearray([9])

    def test_packet_split_across_notifications(self):
        got, c = self._collect()
        raw = Packet.Manager().new_packet(1, 2, None, list(range(30))).build()
        for i in range(0, len(raw), 7):
            c.add(raw[i:i + 7])
        assert len(got) == 1 and list(got[0].data) == list(range(30))

    def test_two_packets_in_one_notification(self):
        got, c = self._collect()
        m = Packet.Manager()
        c.add(m.new_packet(1, 2, None, [1]).build() + m.new_packet(1, 3, None, [2]).build())
        assert [p.cid for p in got] == [2, 3]

    def test_garbage_before_sop_is_ignored(self):
        got, c = self._collect()
        c.add(bytearray([0x00, 0x11, 0x22]) + Packet.Manager().new_packet(1, 2, None, [1]).build())
        assert len(got) == 1

    def test_truncated_packet_resyncs_on_next_sop(self):
        got, c = self._collect()
        m = Packet.Manager()
        good = m.new_packet(1, 2, None, [7]).build()
        truncated = good[:-3]  # lost its tail
        c.add(truncated + m.new_packet(1, 4, None, [8]).build())
        assert [p.cid for p in got] == [4]
