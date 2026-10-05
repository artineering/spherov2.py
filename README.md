# spherov2.py

![status](https://img.shields.io/pypi/status/spherov2?style=for-the-badge) ![python version](https://img.shields.io/pypi/pyversions/spherov2?style=for-the-badge) [![pypi](https://img.shields.io/pypi/v/spherov2?style=for-the-badge)](https://pypi.org/project/spherov2/) [![docs](https://img.shields.io/readthedocs/spherov2?style=for-the-badge)](https://spherov2.readthedocs.io/en/latest/) [![license](https://img.shields.io/pypi/l/spherov2?style=for-the-badge)](LICENSE) ![last commit](https://img.shields.io/github/last-commit/artineering/spherov2.py?style=for-the-badge)

An unofficial Python library for [Sphero](https://sphero.com/) toys that supports its Version 2 Bluetooth low energy API described [here](https://sdk.sphero.com/docs/api_spec/general_api/). Toys that are supported includes (implemented ones are checked):

- [x] Sphero 2.0 / SPRK
- [x] Sphero Ollie
- [x] Sphero BB-8
- [x] Sphero BB-9E
- [x] Sphero R2-D2 / R2-Q5
- [x] Sphero BOLT (In Progress)
- [x] Sphero SPRK+ / SPRK 2.0
- [x] Sphero Mini
- [x] Sphero RVR

Current Progress:
- [ ] Controls
	- [ ] Animation Control
	- [ ] Drive Control
	- [x] LED Control
	- [ ] Sensor Control
	- [ ] Stats Control
	- [ ] Streaming Control

The logic is written based on reverse-engineering the official [Sphero Edu for Android](https://play.google.com/store/apps/details?id=com.sphero.sprk), with the help from available documentation and other unofficial community-based Sphero libraries like [igbopie/spherov2.js](https://github.com/igbopie/spherov2.js) and [EnotYoyo/pysphero](https://github.com/EnotYoyo/pysphero).

This project uses the [hbldh/bleak](https://github.com/hbldh/bleak) Bluetooth Low Energy library, which works across all platforms.

## About this fork

This is a maintained fork of [artificial-intelligence-class/spherov2.py](https://github.com/artificial-intelligence-class/spherov2.py), modernized for current Python and `bleak` releases. Highlights of the upgrade (version `0.13.0`, full details in [CHANGELOG.md](CHANGELOG.md)):

- **Current `bleak` support (0.21 → 3.x)** — `bleak` is now a declared dependency, scanner and GATT write calls use the current keyword APIs, and the TCP relay server no longer crashes on disconnect.
- **Python 3.9 – 3.13** — packaging moved from `setup.py` to `pyproject.toml`; the TCP server runs under `asyncio.run()` so it works on Python 3.12+.
- **Robust connections** — dropped BLE links are detected, pending commands fail fast with `ToyDisconnectedError`, and `toy.add_disconnect_listener()` lets applications react. Unanswered commands raise the builtin `TimeoutError` with the toy and packet id.
- **Packet resynchronisation** — a corrupt or truncated notification no longer desynchronises the v2 packet stream; the collector recovers on the next start-of-packet byte. v2 sequence numbers now wrap correctly at 256.
- **Lighter listener dispatch** — sensor/collision callbacks run on a shared `ThreadPoolExecutor` instead of a new thread per packet, and listener exceptions are logged instead of silently lost.
- **`BleakAdapter` improvements** — `connect_timeout`, `is_connected`, MTU-derived `write_chunk_size`, `read()` and `characteristics()`; scanning also works inside an already-running event loop (Jupyter, GUI apps).
- **Logging** — the library logs through `logging` under the `spherov2` namespace instead of commented-out `print` calls.
- **Tests and CI** — a hardware-free `pytest` suite with an in-memory fake adapter, run with `ruff` on Linux/macOS/Windows × Python 3.9–3.13 via GitHub Actions. Hardware scripts moved to `examples/`.

## Usage

To install the library, run `pip install spherov2`, or install this fork directly with `pip install git+https://github.com/artineering/spherov2.py`. Python `>= 3.9` is supported; the BLE dependency `bleak` is installed automatically.

The library currently has two adapters, `BleakAdapter` and `TCPAdapter`. `BleakAdapter` is used by default when adapter is not specified, which connects to toys using the local Bluetooth adapter. For example:

```python
from spherov2 import scanner

with scanner.find_toy() as toy:
    ...
```

`TCPAdapter` allows the user to send and receive Bluetooth packets connected to another host via a server running on that host as a relay. To start the server, run `python -m spherov2.adapter.tcp_server [host] [port]`, with `host` and `port` by default being `0.0.0.0` and `50004`. To use the adapter, for example:

```python
from spherov2 import scanner
from spherov2.adapter.tcp_adapter import get_tcp_adapter

with scanner.find_toy(adapter=get_tcp_adapter('localhost')) as toy:
    ...
```

The TCP server is written in asynchronous fashion using `asyncio`, so that it supports `bleak` on all platforms.

### Connection loss, timeouts and logging

If a toy stops responding, commands raise the builtin `TimeoutError` (default 10 s, tunable via `toy.response_timeout`). If the BLE link drops, pending and subsequent commands raise `spherov2.toy.ToyDisconnectedError`, and you can be notified with `toy.add_disconnect_listener(callback)`.

The library logs through the standard `logging` module under the `spherov2` namespace. `logging.getLogger('spherov2').setLevel(logging.DEBUG)` prints every packet sent and received, which is useful when reverse-engineering a toy.

### Scanner

You can scan the toys around you using the scanner helper. To find all possible toys, simply call `scanner.find_toys()`. To find only a single toy, use `scanner.find_toy()`.

You can also find toys using specific filters. Please refer to the [document](https://spherov2.readthedocs.io/en/latest/scanner.html) for more information.

### APIs

There are two ways you can interact with the toys, one is to use the low-level APIs implemented for each toy with the commands they support. Low-level APIs can be found for each toy under `spherov2.toy.*`, and is not documented.

The other and recommended way is to use the high level API `spherov2.sphero_edu.SpheroEduAPI`, which is an implementation of the official [Sphero Edu APIs](https://sphero.docsapp.io/docs/get-started). Documentations can be found inside the source files with the docstrings, or [here](https://spherov2.readthedocs.io/en/latest/sphero_edu.html) as an HTML rendered version. For example:

```python
from spherov2 import scanner
from spherov2.sphero_edu import SpheroEduAPI

toy = scanner.find_toy()
with SpheroEduAPI(toy) as api:
    api.spin(360, 1)
```

## Development

```
pip install -e ".[dev]"
ruff check spherov2 tests examples
pytest
```

The unit tests need no hardware: they drive the toy classes through an in-memory fake adapter. Scripts that exercise a real robot are in `examples/`.

## Acknowledgments

This library is made for educational purposes.  It is used by students in [CIS 521 - Artificial Intelligence](http://artificial-intelligence-class.org/) at the University of Pennsylvania, where we use Sphero robots to help teach the foundations of AI.

It is published as an open-source library under the [MIT License](LICENSE).

## Authors

* **Hanbang Wang** - [https://www.cis.upenn.edu/~hanbangw/](https://www.cis.upenn.edu/~hanbangw/)
* **Elionardo Feliciano**

## Maintainers

* **Siddharth Vaghela** - maintainer of the [artineering/spherov2.py](https://github.com/artineering/spherov2.py) fork
