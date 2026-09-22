import struct

import pytest

from spherov2.toy import ToyDisconnectedError
from spherov2.toy.bolt import BOLT
from spherov2.toy.sphero import Sphero
from tests.conftest import Device, FakeAdapter, v1_response_for, v2_response_for


@pytest.fixture
def bolt():
    return BOLT(Device('SB-1234', 'aa:bb'), FakeAdapter)


def echo_ok(uuid, payload):
    if uuid != BOLT._send_uuid:  # handshake writes go to other characteristics
        return []
    return [v2_response_for(payload)]


def test_context_manager_connects_and_cleans_up(bolt):
    FakeAdapter.responder = echo_ok
    with bolt as t:
        assert t.is_connected
        adapter = FakeAdapter.instances[-1]
        assert adapter.callbacks  # subscribed to responses
        t.wake()
        assert adapter.writes[-1][0] == BOLT._send_uuid
    assert adapter.closed and not bolt.is_connected
    with pytest.raises(RuntimeError):
        bolt.wake()


def test_response_data_is_decoded_even_when_chunked(bolt):
    FakeAdapter.chunk = 5

    def responder(uuid, payload):
        if uuid != BOLT._send_uuid:
            return []
        return [v2_response_for(payload, struct.pack('>3H', 4, 2, 1))]
    FakeAdapter.responder = responder
    with bolt:
        v = bolt.get_main_app_version()
    assert (v.major, v.minor, v.revision) == (4, 2, 1)


def test_command_timeout_is_raised_not_hung(bolt):
    bolt.response_timeout = 0.2
    FakeAdapter.responder = lambda u, p: []  # toy never answers
    with bolt:
        with pytest.raises(TimeoutError):
            bolt.wake()


def test_listeners_run_off_ble_thread_and_receive_async_packets(bolt, wait_for):
    import threading
    seen = []
    FakeAdapter.responder = echo_ok
    with bolt:
        bolt.add_charger_state_changed_notify_listener(lambda state: seen.append((state, threading.current_thread().name)))
        # unsolicited notification: DID 0x13 (power) CID 0x21 charger_state_changed_notify; toys use seq 0xff
        from spherov2.controls.v2 import Packet
        pkt = Packet(Packet.Flags.is_activity, 0x13, 0x21, 0xff, None, None, bytearray([1]))
        FakeAdapter.instances[-1].notify(bytes(pkt.build()))
        assert wait_for(lambda: len(seen) == 1)
    state, thread = seen[0]
    assert int(state) == 1 and thread.startswith('spherov2-')


def test_corrupt_notification_does_not_break_stream(bolt):
    FakeAdapter.responder = echo_ok
    with bolt:
        FakeAdapter.instances[-1].notify(b'\x8d\x01\x02\x03\x04\x05\x06\xd8')  # bad checksum
        bolt.wake()  # still works afterwards


def test_disconnect_fails_pending_and_future_commands(bolt, wait_for):
    events = []
    FakeAdapter.responder = lambda u, p: []
    bolt.response_timeout = 5
    with bolt:
        bolt.add_disconnect_listener(lambda: events.append('gone'))
        import threading
        errors = []

        def call():
            try:
                bolt.wake()
            except Exception as e:
                errors.append(e)
        th = threading.Thread(target=call)
        th.start()
        assert wait_for(lambda: len(FakeAdapter.instances[-1].writes) == 2)  # handshake + wake
        FakeAdapter.instances[-1].drop_link()
        th.join(2)
        assert isinstance(errors[0], ToyDisconnectedError)
        assert wait_for(lambda: events == ['gone'])
        assert not bolt.is_connected
        with pytest.raises(ToyDisconnectedError):
            bolt.wake()


def test_failed_connect_propagates():
    FakeAdapter.fail_connect = True
    bolt = BOLT(Device('SB-1', 'x'), FakeAdapter)
    with pytest.raises(ConnectionError):
        with bolt:
            pass


def test_v1_toy_handshake_and_command():
    FakeAdapter.responder = lambda u, p: [v1_response_for(p, b'\x03\x00')] if u == Sphero._send_uuid else []
    toy = Sphero(Device('Sphero-XYZ', 'v1'), FakeAdapter)
    with toy:
        adapter = FakeAdapter.instances[-1]
        assert [u for u, _ in adapter.writes[:2]] == [u for u, _ in Sphero._handshake]
        toy.ping()
