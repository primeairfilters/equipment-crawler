"""Which addresses the fetcher may connect to: public ones only."""
import ipaddress

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_COMPAT = ipaddress.ip_network("::/96")


def is_public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version == 6:
        embedded = ip.ipv4_mapped
        if embedded is None and (ip in _NAT64 or ip in _COMPAT):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_ip(str(embedded))
        if ip.is_site_local or ip.is_reserved:
            return False
    return ip.is_global and not ip.is_multicast
