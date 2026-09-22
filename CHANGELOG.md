# Changelog

## 0.13.0 (unreleased)

### Fixed
- `bleak` was never declared as a dependency; `pip install spherov2` produced an `ImportError` on first scan.
- Compatibility with current `bleak` (0.21 → 3.x): scanner calls now use keyword arguments, `write_gatt_char`
  passes `response=` explicitly, and `tcp_server` no longer calls the `is_connected` property as a coroutine
  (which raised `TypeError` on every disconnect).
- `tcp_server` used the deprecated `asyncio.get_event_loop()` pattern that breaks on Python 3.12+; it now runs
  under `asyncio.run()` and tolerates devices advertising without a name.
- `BleakAdapter.close()` could leave the background event-loop thread running forever if `disconnect()` raised.
- A command whose response never arrived blocked for the timeout and then raised `concurrent.futures.TimeoutError`
  from deep inside the library; it now raises the builtin `TimeoutError` with the toy and packet id in the message.
- A toy that dropped its BLE link mid-session left callers hanging. Disconnects are now detected, pending commands fail
  fast with `ToyDisconnectedError`, and `Toy.add_disconnect_listener()` lets applications react.
- One corrupt or truncated BLE notification permanently desynchronised the v2 packet collector; it now resynchronises
  on the next start-of-packet byte and logs the dropped frame.
- v2 sequence numbers wrapped at 255 instead of 256.
- Duplicate `IntEnum` definitions in `commands/*` that shadowed the canonical ones in `listeners/*`.

### Changed
- Listener callbacks (sensor streaming, collisions, ...) run on a small `ThreadPoolExecutor` instead of spawning a new
  OS thread per packet, and exceptions in user listeners are logged rather than lost.
- `BleakAdapter` accepts `connect_timeout` and exposes `is_connected`, `write_chunk_size` (derived from the negotiated
  MTU), `read()` and `characteristics()`.
- Scanning works from inside an already-running asyncio loop (Jupyter, GUI apps).
- `ToyNotFoundError` now says what it was looking for.
- Packaging moved from `setup.py` to `pyproject.toml`; minimum Python is 3.9.
- Library uses `logging` (`spherov2.*` loggers) instead of commented-out `print` calls; set `DEBUG` to see raw
  TX/RX packets.

### Added
- A hardware-free unit test suite (`tests/`) with an in-memory fake adapter, run on Linux/macOS/Windows × Python
  3.9–3.13 in CI. The old hardware scripts live in `examples/`.
