"""Shared test helpers: an in-memory adapter that emulates a Sphero over a fake GATT link."""
import threading
from typing import NamedTuple

import pytest

from spherov2.controls.v2 import Packet as PacketV2
from spherov2.helper import packet_chk


class Device(NamedTuple):
    name: str
    address: str


class FakeAdapter:
    """Adapter that records writes and lets a test script the toy's replies.

    ``responder(uuid, payload) -> list[bytes] | None`` is called for every write; whatever it returns is delivered
    back through the notification callback, optionally split into ``chunk``-sized notifications like a real BLE link.
    """

    instances = []
    responder = None
    chunk = None
    fail_connect = False

    @classmethod
    def scan_toys(cls, timeout=5.0):
        return cls.devices

    @classmethod
    def scan_toy(cls, name, timeout=5.0):
        return next((d for d in cls.devices if d.name == name), None)

    devices = []

    def __init__(self, address, *, on_disconnect=None):
        if FakeAdapter.fail_connect:
            raise ConnectionError('boom')
        self.address = address
        self.on_disconnect = on_disconnect
        self.writes = []
        self.callbacks = {}
        self.closed = False
        self.is_connected = True
        self.write_chunk_size = 20
        FakeAdapter.instances.append(self)

    def close(self, disconnect=True):
        self.closed = True
        self.is_connected = False

    def set_callback(self, uuid, cb):
        self.callbacks[uuid] = cb

    def write(self, uuid, data):
        if not self.is_connected:
            raise ConnectionError('link down')
        self.writes.append((uuid, bytes(data)))
        if FakeAdapter.responder is None:
            return
        replies = FakeAdapter.responder(uuid, bytes(data)) or []
        for reply in replies:
            self.notify(reply)

    def notify(self, data):
        """Deliver bytes to every subscribed callback, chunked if ``chunk`` is set."""
        chunk = FakeAdapter.chunk or len(data) or 1
        for cb in list(self.callbacks.values()):
            for i in range(0, len(data), chunk):
                cb(None, bytearray(data[i:i + chunk]))

    def drop_link(self):
        """Simulate the toy going away."""
        self.is_connected = False
        if self.on_disconnect:
            self.on_disconnect()


def v2_response_for(request: bytes, data=b'', err=PacketV2.Error.success) -> bytes:
    """Build the v2 response packet a toy would send for ``request``."""
    req = PacketV2.parse_response(list(request))
    flags = (req.flags | PacketV2.Flags.is_response) & ~PacketV2.Flags.requests_response
    tid, sid = req.sid, req.tid  # response swaps source/target
    return bytes(PacketV2(flags, req.did, req.cid, req.seq, tid, sid, bytearray(data), err).build())


def v1_response_for(request: bytes, data=b'', mrsp=0) -> bytes:
    seq = request[4]
    payload = bytearray([0xff, 0xff, mrsp, seq, len(data) + 1, *data])
    payload.append(packet_chk(payload[2:]))
    return bytes(payload)


@pytest.fixture(autouse=True)
def _reset_fake_adapter():
    FakeAdapter.instances = []
    FakeAdapter.responder = None
    FakeAdapter.chunk = None
    FakeAdapter.fail_connect = False
    FakeAdapter.devices = []
    yield


@pytest.fixture
def wait_for():
    """``wait_for(predicate, timeout)`` polls until the predicate holds, for asserting on worker-thread side effects."""
    def _wait(pred, timeout=2.0):
        ev = threading.Event()
        deadline = timeout
        step = 0.01
        while deadline > 0:
            if pred():
                return True
            ev.wait(step)
            deadline -= step
        return pred()
    return _wait
