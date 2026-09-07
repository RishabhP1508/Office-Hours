"""Start uvicorn on a dual-stack socket.

Fly needs both: fly-proxy reaches the public service over IPv4, and `.internal` (6PN) is IPv6-only,
so the gateway can only reach this app over IPv6. Neither `--host 0.0.0.0` nor `--host ::` serves
both, because asyncio's create_server sets IPV6_V6ONLY on any AF_INET6 listener it makes. Binding
the socket here with dualstack_ipv6=True and handing it to uvicorn is what covers both families.
"""

import os
import socket

import uvicorn


def main() -> None:
    port = int(os.environ.get("UVICORN_PORT", "8000"))
    sock = socket.create_server(
        ("::", port), family=socket.AF_INET6, dualstack_ipv6=True, reuse_port=False
    )
    config = uvicorn.Config("app.main:app", log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"))
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()