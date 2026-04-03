"""
GenCall - Because Sigma had its time.
"""

VERSION = "2.0.0"

BANNER = r"""
   ██████╗ ███████╗███╗   ██╗ ██████╗ █████╗ ██╗     ██╗
  ██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔══██╗██║     ██║
  ██║  ███╗█████╗  ██╔██╗ ██║██║     ███████║██║     ██║
  ██║   ██║██╔══╝  ██║╚██╗██║██║     ██╔══██║██║     ██║
  ╚██████╔╝███████╗██║ ╚████║╚██████╗██║  ██║███████╗███████╗
   ╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝

  ┌─────────────────────────────────────────────────────┐
  │  SIP Traffic Generator v{version}                      │
  │  The Sigma killer. Built different.                 │
  │                                                     │
  │  Dashboard : http://{host}:{port:<5}                   │
  │  API       : http://{host}:{port:<5}/api               │
  │  WebSocket : ws://{host}:{port:<5}/ws                  │
  │  Docs      : http://{host}:{port:<5}/docs              │
  └─────────────────────────────────────────────────────┘
"""

BANNER_COMPACT = r"""
  ╔═╗╔═╗╔╗╔╔═╗╔═╗╦  ╦
  ║ ╦║╣ ║║║║  ╠═╣║  ║  v{version}
  ╚═╝╚═╝╝╚╝╚═╝╩ ╩╩═╝╩═╝
"""


def print_banner(host: str = "0.0.0.0", port: int = 8080, compact: bool = False):
    if compact:
        print(BANNER_COMPACT.format(version=VERSION))
    else:
        print(BANNER.format(version=VERSION, host=host, port=port))


MOTD_LINES = [
    "Sigma walked so GenCall could run.",
    "24 if-else blocks? We don't do that here.",
    "Your traffic profiles now actually work. You're welcome.",
    "randnum != 1 and randnum != 2 and ... LOL never again.",
    "Built from scratch. No .so files. No secrets. Pure Python.",
    "Copy-paste is not a design pattern. Dataclasses are.",
    "500K row CSV iteration for one number pick? That's violence.",
    "try/finally: because RTP streams deserve a proper goodbye.",
]


def random_motd() -> str:
    import random
    return random.choice(MOTD_LINES)
