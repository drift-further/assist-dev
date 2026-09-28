"""Which addresses the server listens on.

Loopback always: the CLI, /health and any reverse proxy talk to 127.0.0.1.
ASSIST_BIND adds ONE more address -- the host's LAN address, written by
`assist expose` -- so a phone can reach Flask without a proxy. It must be a
specific address: a wildcard (0.0.0.0, ::) would put every endpoint on every
interface, including ones the owner never meant to serve, so it is refused
rather than honoured.
"""

import ipaddress


def bind_addresses(host, extra):
    """Return the listen addresses: `host` first, then ASSIST_BIND if distinct.

    Raises ValueError for a wildcard, a hostname, or anything unparseable.
    """
    addresses = [host]
    extra = (extra or "").strip()
    if not extra:
        return addresses
    try:
        address = ipaddress.ip_address(extra)
    except ValueError:
        raise ValueError(f"ASSIST_BIND must be an IP address, not {extra!r}") from None
    if address.is_unspecified:
        raise ValueError(
            f"ASSIST_BIND={extra} would listen on every interface; "
            "use the LAN address (assist expose picks it)"
        )
    if address.is_loopback or str(address) == host:
        return addresses
    addresses.append(str(address))
    return addresses
