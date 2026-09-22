import pytest

from spherov2 import scanner
from spherov2.toy.bolt import BOLT
from spherov2.toy.mini import Mini
from spherov2.toy.rvr import RVR
from tests.conftest import Device, FakeAdapter


def test_find_toys_maps_names_to_classes():
    FakeAdapter.devices = [Device('SB-1234', 'aa'), Device('SM-9999', 'bb'), Device('RV-0001', 'cc'),
                           Device(None, 'dd'), Device('Kitchen Speaker', 'ee')]
    toys = scanner.find_toys(adapter=FakeAdapter)
    assert {type(t) for t in toys} == {BOLT, Mini, RVR}


def test_find_toy_filters_by_type_and_name():
    FakeAdapter.devices = [Device('SB-1234', 'aa'), Device('SB-5678', 'bb')]
    toy = scanner.find_toy(toy_name='SB-5678', adapter=FakeAdapter)
    assert isinstance(toy, BOLT) and toy.address == 'bb'
    assert scanner.find_BOLT(adapter=FakeAdapter).address == 'aa'


def test_find_toy_raises_helpful_error():
    with pytest.raises(scanner.ToyNotFoundError, match='Sphero BOLT'):
        scanner.find_BOLT(adapter=FakeAdapter)
