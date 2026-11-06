import asyncio
import inspect

import bleak
import pytest

from spherov2.adapter import bleak_adapter


def test_run_works_with_and_without_running_loop():
    async def coro():
        await asyncio.sleep(0)
        return 42

    assert bleak_adapter._run(coro()) == 42

    async def outer():
        # e.g. Jupyter: a loop is already running on this thread; asyncio.run() would raise
        return bleak_adapter._run(coro())

    assert asyncio.run(outer()) == 42


def test_scan_calls_use_keyword_arguments(monkeypatch):
    seen = {}

    async def discover(**kw):
        seen['discover'] = kw
        return []

    async def find(filterfunc, **kw):
        seen['find'] = kw
        return None

    monkeypatch.setattr(bleak.BleakScanner, 'discover', staticmethod(discover))
    monkeypatch.setattr(bleak.BleakScanner, 'find_device_by_filter', staticmethod(find))
    bleak_adapter.BleakAdapter.scan_toys(2.5)
    bleak_adapter.BleakAdapter.scan_toy('SB-1', 1.5)
    assert seen == {'discover': {'timeout': 2.5}, 'find': {'timeout': 1.5}}


def test_adapter_matches_current_bleak_api():
    """Guard against bleak signature drift: every kwarg we pass must exist."""
    assert 'disconnected_callback' in inspect.signature(bleak.BleakClient.__init__).parameters
    assert 'response' in inspect.signature(bleak.BleakClient.write_gatt_char).parameters
    assert isinstance(bleak.BleakClient.is_connected, property)


def test_connect_failure_closes_loop_and_raises(monkeypatch):
    class FailingClient:
        is_connected = False

        def __init__(self, address, **kw):
            pass

        async def connect(self):
            raise bleak.BleakError('nope')

    monkeypatch.setattr(bleak, 'BleakClient', FailingClient)
    with pytest.raises(bleak.BleakError):
        bleak_adapter.BleakAdapter('aa:bb', connect_timeout=1)


# ---------------------------------------------------------------------------------------------- connection lifecycle
#
# FakeBleakClient mimics the bleak behaviours that matter for teardown:
#  * a cancelled connect() needs further loop iterations to undo the partial link (BlueZ awaits a D-Bus Disconnect);
#  * connect() leaves a background task that sends Disconnect when cancelled (BlueZ's disconnect monitor);
#  * disconnect() fires disconnected_callback *before* it returns, even for a requested disconnect.

import threading  # noqa: E402
import time  # noqa: E402


def _ble_threads():
    return [t for t in threading.enumerate() if t.name.startswith('spherov2-ble-') and t.is_alive()]


class FakeBleakClient:
    connect_delay = 0.0
    log = []

    def __init__(self, address, disconnected_callback=None, timeout=None):
        self._cb = disconnected_callback
        self.is_connected = False
        FakeBleakClient.last = self

    async def connect(self):
        try:
            await asyncio.sleep(self.connect_delay)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # cleanup that needs loop time
            FakeBleakClient.log.append('connect-cleanup')
            raise
        self.is_connected = True
        asyncio.get_running_loop().create_task(self._monitor())

    async def _monitor(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            FakeBleakClient.log.append('monitor-cleanup')
            raise

    async def disconnect(self):
        FakeBleakClient.log.append('disconnect')
        if self.is_connected:
            self.is_connected = False
            if self._cb:
                self._cb(self)

    async def write_gatt_char(self, uuid, data, response=True):
        FakeBleakClient.log.append('write')

    def drop(self):
        self.is_connected = False
        self._cb(self)


@pytest.fixture
def fake_client(monkeypatch):
    FakeBleakClient.connect_delay = 0.0
    FakeBleakClient.log = []
    monkeypatch.setattr(bleak, 'BleakClient', FakeBleakClient)
    before = set(_ble_threads())
    yield FakeBleakClient
    deadline = time.monotonic() + 2
    while set(_ble_threads()) - before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not set(_ble_threads()) - before, 'BLE loop thread leaked'


def test_connect_timeout_lets_bleak_clean_up(fake_client):
    fake_client.connect_delay = 10
    with pytest.raises(TimeoutError):
        bleak_adapter.BleakAdapter('aa:bb', connect_timeout=0.2)
    # bleak's cancellation cleanup ran to completion, and we explicitly disconnected afterwards
    assert fake_client.log == ['connect-cleanup', 'disconnect']


def test_close_lets_background_tasks_finish(fake_client):
    adapter = bleak_adapter.BleakAdapter('aa:bb')
    adapter.close()
    assert 'disconnect' in fake_client.log
    assert 'monitor-cleanup' in fake_client.log


def test_requested_disconnect_is_not_reported_as_a_drop(fake_client):
    calls = []
    adapter = bleak_adapter.BleakAdapter('aa:bb', on_disconnect=lambda: calls.append(1))
    adapter.close()
    assert calls == []


def test_unexpected_drop_is_reported(fake_client):
    calls = []
    adapter = bleak_adapter.BleakAdapter('aa:bb', on_disconnect=lambda: calls.append(1))
    fake_client.last.drop()
    assert calls == [1]
    adapter.close()


def test_client_construction_failure_does_not_leak_loop_thread(monkeypatch, fake_client):
    def boom(*a, **kw):
        raise bleak.BleakError('no backend')

    monkeypatch.setattr(bleak, 'BleakClient', boom)
    with pytest.raises(bleak.BleakError):
        bleak_adapter.BleakAdapter('aa:bb')


def test_concurrent_close_is_safe(fake_client):
    adapter = bleak_adapter.BleakAdapter('aa:bb')
    errors = []

    def close():
        try:
            adapter.close()
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=close) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with pytest.raises(ConnectionError):
        adapter.write('x', b'\x00')
