"""Base classes shared by every supported toy."""
from __future__ import annotations

import inspect
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from concurrent import futures
from functools import partial
from queue import SimpleQueue
from typing import Callable, NamedTuple, Optional

from spherov2.controls import PacketDecodingException
from spherov2.controls.v1 import Packet as PacketV1
from spherov2.controls.v2 import Packet as PacketV2
from spherov2.types import ToyType

logger = logging.getLogger(__name__)


class ToySensor(NamedTuple):
    bit: int
    min_value: float
    max_value: float
    modifier: Optional[Callable[[float], float]] = None


class ToyDisconnectedError(ConnectionError):
    """Raised when a command is issued after the toy dropped the BLE link."""


class Toy:
    """A connected (or connectable) Sphero toy.

    Use it as a context manager; entering opens the BLE link and starts the packet writer thread, exiting tears both
    down. Low-level commands (``toy.wake()``, ``toy.drive_with_heading(...)``) are attached by each subclass; the high
    level :class:`spherov2.sphero_edu.SpheroEduAPI` is the recommended entry point.
    """

    toy_type = ToyType('Robot', None, 'Sphero', .06)
    sensors = OrderedDict()
    extended_sensors = OrderedDict()

    _send_uuid = '22bb746f-2ba1-7554-2d6f-726568705327'
    _response_uuid = '22bb746f-2ba6-7554-2d6f-726568705327'
    _handshake = [('22bb746f-2bbd-7554-2d6f-726568705327', bytearray(b'011i3')),
                  ('22bb746f-2bb2-7554-2d6f-726568705327', bytearray([7]))]
    _packet = PacketV1
    _require_target = False

    #: Default seconds to wait for a command response.
    response_timeout: float = 10.0
    #: Threads used to run user listeners (sensor streaming, collisions...). Listeners never run on the BLE thread.
    listener_workers: int = 4

    def __init__(self, toy, adapter_cls):
        self.address = toy.address
        self.name = toy.name

        self.__adapter = None
        self.__adapter_cls = adapter_cls
        self._packet_manager = self._packet.Manager()
        self.__decoder = self._packet.Collector(self.__new_packet)
        self.__waiting = defaultdict(SimpleQueue)
        self.__listeners = defaultdict(dict)
        self.__disconnect_listeners = set()
        self._sensor_controller = None

        self.__thread = None
        self.__pool = None
        self.__packet_queue = SimpleQueue()
        self.__disconnected = threading.Event()

    def __repr__(self):
        return f'{self.name} ({self.address})'

    # ------------------------------------------------------------------ connection lifecycle

    @property
    def is_connected(self) -> bool:
        return self.__adapter is not None and not self.__disconnected.is_set() and \
            getattr(self.__adapter, 'is_connected', True)

    def add_disconnect_listener(self, listener: Callable[[], None]):
        """Register ``listener()`` to be called (on a worker thread) if the toy drops the connection."""
        self.__disconnect_listeners.add(listener)

    def remove_disconnect_listener(self, listener: Callable[[], None]):
        self.__disconnect_listeners.discard(listener)

    def __enter__(self):
        if self.__adapter is not None:
            raise RuntimeError('Toy already in context manager')
        self.__disconnected.clear()
        self.__packet_queue = SimpleQueue()
        self.__pool = futures.ThreadPoolExecutor(max_workers=self.listener_workers,
                                                 thread_name_prefix=f'spherov2-{self.name}')
        self.__adapter = self.__adapter_cls(self.address, **self.__adapter_kwargs())
        self.__thread = threading.Thread(target=self.__process_packet, name=f'spherov2-tx-{self.name}', daemon=True)
        try:
            for uuid, data in self._handshake:
                self.__adapter.write(uuid, data)
            self.__adapter.set_callback(self._response_uuid, self.__api_read)
            self.__thread.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __adapter_kwargs(self):
        """Pass ``on_disconnect`` only to adapters that accept it, so third-party adapters keep working."""
        try:
            params = inspect.signature(self.__adapter_cls).parameters
        except (TypeError, ValueError):
            return {}
        if 'on_disconnect' in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return {'on_disconnect': self.__on_adapter_disconnect}
        return {}

    def __exit__(self, exc_type, exc_val, exc_tb):
        adapter, self.__adapter = self.__adapter, None
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                logger.warning('Error closing adapter', exc_info=True)
        if self.__thread is not None and self.__thread.is_alive():
            self.__packet_queue.put(None)
            self.__thread.join(timeout=self.response_timeout)
        self.__thread = None
        # fail anything still waiting for a response so callers don't block for the full timeout
        self.__fail_waiters(ToyDisconnectedError('Toy connection closed'))
        if self.__pool is not None:
            self.__pool.shutdown(wait=False)
            self.__pool = None
        self.__packet_queue = SimpleQueue()

    def __on_adapter_disconnect(self):
        if self.__disconnected.is_set():
            return
        self.__disconnected.set()
        logger.warning('%s disconnected unexpectedly', self)
        self.__fail_waiters(ToyDisconnectedError(f'{self} disconnected'))
        for listener in list(self.__disconnect_listeners):
            self.__submit(listener)

    def __fail_waiters(self, exc: Exception):
        for queue in list(self.__waiting.values()):
            while not queue.empty():
                fut = queue.get()
                if not fut.done():
                    fut.set_exception(exc)

    # ------------------------------------------------------------------ transmit path

    def __process_packet(self):
        chunk = getattr(self.__adapter, 'write_chunk_size', 20)
        while self.__adapter is not None:
            payload = self.__packet_queue.get()
            if payload is None:
                break
            logger.debug('TX %s', payload.hex())
            try:
                while payload:
                    self.__adapter.write(self._send_uuid, payload[:chunk])
                    payload = payload[chunk:]
            except Exception:
                if self.__adapter is not None and not self.__disconnected.is_set():
                    logger.exception('Failed to write packet to %s', self)
                    self.__on_adapter_disconnect()
                # whoever queued this packet is waiting on it; make sure they hear about it
                self.__fail_waiters(ToyDisconnectedError(f'{self} is disconnected'))
                break
            time.sleep(self.toy_type.cmd_safe_interval)

    def _execute(self, packet, timeout: Optional[float] = None):
        if self.__adapter is None:
            raise RuntimeError('Use toys in context manager')
        if self.__disconnected.is_set():
            raise ToyDisconnectedError(f'{self} is disconnected')
        # register the waiter *before* sending so a fast response can never be missed
        future = self.__register_waiter(packet.id)
        self.__packet_queue.put(packet.build())
        if self.__disconnected.is_set() and not future.done():
            # disconnect raced with registration; the sweep in __on_adapter_disconnect may have missed us
            future.set_exception(ToyDisconnectedError(f'{self} is disconnected'))
        return self.__await(packet.id, future, timeout)

    def __register_waiter(self, key):
        future = futures.Future()
        self.__waiting[key].put(future)
        return future

    def __await(self, key, future, timeout, check_error=False):
        try:
            packet = future.result(self.response_timeout if timeout is None else timeout)
        except futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f'No response from {self} for packet {key}') from None
        if check_error:
            packet.check_error()
        return packet

    def _wait_packet(self, key, timeout: Optional[float] = None, check_error=False):
        return self.__await(key, self.__register_waiter(key), timeout, check_error)

    # ------------------------------------------------------------------ listeners

    def _add_listener(self, key, listener: Callable):
        self.__listeners[key[0]][listener] = partial(key[1], listener)

    def _remove_listener(self, key, listener: Callable):
        self.__listeners[key[0]].pop(listener, None)

    def __submit(self, fn, *args):
        pool = self.__pool
        if pool is None:
            return
        try:
            pool.submit(self.__guarded, fn, *args)
        except RuntimeError:  # pool already shut down
            pass

    @staticmethod
    def __guarded(fn, *args):
        try:
            fn(*args)
        except Exception:
            logger.exception('Listener %r raised', fn)

    # ------------------------------------------------------------------ receive path (runs on the adapter thread)

    def __api_read(self, char, data):
        try:
            self.__decoder.add(bytearray(data))
        except PacketDecodingException as e:
            # a corrupt notification must not poison the stream; log and keep going
            logger.warning('Dropping undecodable packet from %s: %s', self, e)
        except Exception:
            logger.exception('Unexpected error decoding packet from %s', self)

    def __new_packet(self, packet):
        logger.debug('RX %r', packet)
        key = packet.id
        queue = self.__waiting[key]
        while not queue.empty():
            fut = queue.get()
            if not fut.cancelled():
                fut.set_result(packet)
        for f in list(self.__listeners[key].values()):
            self.__submit(f, packet)

    @classmethod
    def implements(cls, method, with_target=False):
        m = getattr(cls, method.__name__, None)
        if m is method:
            return with_target == cls._require_target
        if hasattr(m, '_partialmethod'):
            f = m._partialmethod
            return f.func is method and (
                    ('proc' in f.keywords and not with_target) or with_target == cls._require_target)
        return False


class ToyV2(Toy):
    _packet = PacketV2
    _handshake = []

    _response_uuid = _send_uuid = '00010002-574f-4f20-5370-6865726f2121'
