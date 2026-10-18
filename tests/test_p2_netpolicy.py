"""P2: the real address policy used by the fetcher. Pure, no network."""
import pytest


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.1.2.3", "::1",
    "10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.1",
    "169.254.169.254", "169.254.0.1", "fe80::1",
    "100.64.0.1", "100.127.255.255",
    "fc00::1", "fd12:3456::1",
    "0.0.0.0", "::",
    "224.0.0.1", "255.255.255.255",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:10.0.0.1",
    "not an ip", "",
])
def test_is_public_ip_refuses_internal_and_odd_addresses(ip):
    from crawler.netpolicy import is_public_ip
    assert is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "172.32.0.1", "100.128.0.1", "2606:4700:4700::1111"])
def test_is_public_ip_allows_public_addresses(ip):
    from crawler.netpolicy import is_public_ip
    assert is_public_ip(ip) is True
