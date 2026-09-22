from spherov2.controls import PacketDecodingException
from spherov2.controls.v1 import Packet
from spherov2.helper import packet_chk
import pytest


def test_request_build_layout():
    p = Packet.Manager().new_packet(0x02, 0x30, None, [1, 2])
    raw = p.build()
    assert raw[:2] == b'\xff\xff' and raw[2] == 0x02 and raw[3] == 0x30 and raw[5] == 3
    assert raw[-1] == packet_chk(raw[2:-1])


def test_collector_response_and_async_mixed_and_split():
    got = []
    c = Packet.Collector(got.append)
    resp = Packet.Response(Packet.Error.command_succeeded, 5, bytearray([1, 2])).build()
    asyn = Packet.Async(0x03, bytearray([0, 1, 0, 2])).build()
    stream = bytearray([0x42]) + resp + asyn  # leading junk byte
    for i in range(0, len(stream), 3):
        c.add(stream[i:i + 3])
    assert len(got) == 2
    assert isinstance(got[0], Packet.Response) and got[0].seq == 5 and got[0].data == bytearray([1, 2])
    assert isinstance(got[1], Packet.Async) and got[1].id_code == 3


def test_collector_recovers_after_bad_frame():
    got = []
    c = Packet.Collector(got.append)
    with pytest.raises(PacketDecodingException):
        c.add(bytearray([0xff, 0x00, 1, 2, 3]))
    c.add(Packet.Response(Packet.Error.command_succeeded, 1, bytearray()).build())
    assert len(got) == 1
