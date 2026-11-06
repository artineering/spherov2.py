"""Bluetooth Low Energy transport built on `bleak <https://github.com/hbldh/bleak>`_.

The rest of the library is synchronous and thread based. This adapter owns a private asyncio event loop running in a
background thread and marshals every bleak call onto it, so callers never have to touch asyncio. Notifications are
delivered on the loop thread; the :class:`spherov2.toy.Toy` layer hands them off to its own worker pool.

Compatible with bleak 0.21 through 3.x (keyword arguments only, ``is_connected`` as a property).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Callable, Iterable, Optional

import bleak

logger = logging.getLogger(__name__)

# Default write chunk when the MTU cannot be determined. 20 bytes is the ATT default (23 - 3 header bytes).
_DEFAULT_CHUNK = 20


def _run(coro, timeout: Optional[float] = None):
    """Run a coroutine to completion from synchronous code.

    ``asyncio.run`` refuses to work when an event loop is already running on the current thread (Jupyter, GUI
    frameworks, an outer asyncio program). In that case the coroutine is executed on a throwaway loop in a helper
    thread instead, which keeps the blocking API usable everywhere.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout)


class BleakAdapter:
    """Connects to a toy through the local Bluetooth adapter.

    Instances are created by :class:`spherov2.toy.Toy` when entering its context manager; users only pass the class
    (``adapter=BleakAdapter``) or subclass it to tweak :attr:`connect_timeout` / :attr:`command_timeout`.
    """

    #: Seconds to wait for the BLE connection (including bleak's device lookup) to be established.
    connect_timeout: float = 15.0
    #: Seconds to wait for any single GATT operation (write / subscribe / disconnect) before giving up.
    command_timeout: float = 10.0
    #: Seconds allowed for tearing down a failed or closing connection before the loop is stopped anyway.
    cleanup_timeout: float = 5.0

    # ------------------------------------------------------------------ scanning (class-level, no instance needed)

    @staticmethod
    def scan_toys(timeout: float = 5.0):
        """Return every advertising BLE device seen within ``timeout`` seconds."""
        return _run(bleak.BleakScanner.discover(timeout=timeout))

    @staticmethod
    def scan_toy(name: str, timeout: float = 5.0):
        """Return the first device advertising exactly ``name``, or ``None`` if none is seen within ``timeout``."""
        return _run(bleak.BleakScanner.find_device_by_filter(
            lambda _, adv: adv.local_name == name, timeout=timeout))

    # ------------------------------------------------------------------ connection lifecycle

    def __init__(self, address, *, connect_timeout: Optional[float] = None,
                 on_disconnect: Optional[Callable[[], None]] = None):
        if connect_timeout is not None:
            self.connect_timeout = connect_timeout
        self.__address = address
        self.__on_disconnect = on_disconnect
        # on_disconnect is only reported between a successful connect (__ready) and close() (__closing);
        # __closed refuses new work
        self.__ready = False
        self.__closing = False
        self.__closed = False
        self.__lock = threading.Lock()
        self.__close_lock = threading.Lock()
        self.__device = None
        self.__loop = asyncio.new_event_loop()
        self.__thread = threading.Thread(target=self.__run_loop, name=f'spherov2-ble-{address}', daemon=True)
        self.__thread.start()
        try:
            self.__device = bleak.BleakClient(address, disconnected_callback=self.__disconnected,
                                              timeout=self.connect_timeout)
            # __connect enforces connect_timeout on the loop itself; this outer limit is only a safety net
            self.__execute(self.__connect(), self.connect_timeout + self.cleanup_timeout + 5)
        except BaseException:
            self.close(disconnect=False)  # __connect has already torn down any partial link
            raise

    def __run_loop(self):
        try:
            self.__loop.run_forever()
        finally:
            self.__loop.close()

    async def __connect(self):
        """Connect, and on *any* failure undo whatever part of the link came up before re-raising.

        The timeout has to be enforced here, on the loop, rather than by the calling thread: cancelling from outside
        and then stopping the loop never gives bleak the loop time it needs to run its own cleanup (on BlueZ, the
        D-Bus ``Disconnect`` that cancels a pending or half-finished connection). The OS then keeps the toy
        connected, it stops advertising, and every later connect attempt times out as well.
        """
        try:
            await asyncio.wait_for(self.__device.connect(), self.connect_timeout)
        except BaseException as e:
            await self.__disconnect_quietly()
            if isinstance(e, asyncio.TimeoutError):  # distinct from the builtin before Python 3.11
                raise TimeoutError(f'Could not connect to {self.__address} within {self.connect_timeout}s') from None
            raise
        self.__ready = True

    async def __disconnect_quietly(self):
        try:
            await asyncio.wait_for(self.__device.disconnect(), self.cleanup_timeout)
        except (Exception, asyncio.CancelledError):
            logger.warning('Error while disconnecting from %s', self.__address, exc_info=True)

    async def __cancel_pending_tasks(self):
        """Cancel and await everything still running on the loop so cleanup handlers get to finish.

        This includes bleak's own background tasks (e.g. the BlueZ disconnect monitor, which sends ``Disconnect`` when
        cancelled), which would otherwise be destroyed mid-flight by stopping the loop.
        """
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self.cleanup_timeout)

    def __disconnected(self, _client):
        logger.info('BLE device disconnected')
        if self.__on_disconnect is not None and self.__ready and not self.__closing:
            try:
                self.__on_disconnect()
            except Exception:  # user callback; never let it kill the bleak loop
                logger.exception('on_disconnect callback raised')

    def __execute(self, coroutine, timeout: Optional[float] = None):
        with self.__lock:
            if self.__closed:
                coroutine.close()
                raise ConnectionError('Adapter is closed')
            future = asyncio.run_coroutine_threadsafe(coroutine, self.__loop)
        try:
            return future.result(timeout if timeout is not None else self.command_timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError('BLE operation timed out') from None
        except concurrent.futures.CancelledError:
            raise ConnectionError('Adapter is closed') from None

    @property
    def is_connected(self) -> bool:
        return not self.__closed and self.__device is not None and bool(self.__device.is_connected)

    @property
    def write_chunk_size(self) -> int:
        """Largest payload the toy's command characteristic accepts per write."""
        try:
            mtu = self.__device.mtu_size
        except Exception:
            return _DEFAULT_CHUNK
        return max(_DEFAULT_CHUNK, (mtu or 23) - 3)

    def close(self, disconnect: bool = True):
        """Tear down the connection and the background loop. Safe to call more than once, from any thread."""
        with self.__close_lock:
            if self.__closed:
                return
            self.__closing = True
            on_loop = threading.current_thread() is self.__thread
            try:
                if disconnect and not on_loop and self.__device is not None and self.__device.is_connected:
                    self.__execute(self.__disconnect_quietly(), self.cleanup_timeout + 5)
            except Exception:
                logger.warning('Error while disconnecting; continuing shutdown', exc_info=True)
            finally:
                with self.__lock:
                    self.__closed = True
                if on_loop:
                    # called from a bleak callback: we can't block on our own loop, so just let it wind down
                    self.__loop.call_soon(self.__loop.stop)
                else:
                    self.__stop_loop()

    def __stop_loop(self):
        try:
            asyncio.run_coroutine_threadsafe(self.__cancel_pending_tasks(), self.__loop) \
                .result(self.cleanup_timeout + 1)
        except Exception:
            logger.warning('Pending BLE tasks did not finish cleanly', exc_info=True)
        self.__loop.call_soon_threadsafe(self.__loop.stop)
        self.__thread.join(timeout=self.command_timeout)

    # ------------------------------------------------------------------ GATT operations

    def set_callback(self, uuid, cb):
        """Subscribe to notifications on ``uuid``. ``cb(characteristic, data: bytearray)`` runs on the loop thread."""
        self.__execute(self.__device.start_notify(uuid, cb))

    def write(self, uuid, data):
        self.__execute(self.__device.write_gatt_char(uuid, data, response=True))

    def read(self, uuid) -> bytearray:
        return self.__execute(self.__device.read_gatt_char(uuid))

    def characteristics(self) -> Iterable[str]:
        """UUIDs of every characteristic on the connected device (handy when reverse engineering a new toy)."""
        return [c.uuid for s in self.__device.services for c in s.characteristics]
