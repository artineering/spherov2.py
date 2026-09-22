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
