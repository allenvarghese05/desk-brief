import socket

import pytest

import deskbrief


def test_version_is_set() -> None:
    assert deskbrief.__version__ == "0.1.0"


def test_network_is_blocked_in_unit_tests() -> None:
    with pytest.raises(Exception, match=r"(?i)socket"):
        socket.create_connection(("example.com", 80), timeout=1)
