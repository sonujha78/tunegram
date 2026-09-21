import time
from dataclasses import dataclass

import psutil

_START = time.monotonic()


@dataclass(frozen=True)
class Stats:
    uptime: str
    disk: float
    cpu: float
    ram: float


def prime() -> None:
    # pehli cpu_percent call hamesha 0.0 deti hai, isliye startup pe ek baar bula lo
    psutil.cpu_percent(interval=None)


def snapshot() -> Stats:
    secs = int(time.monotonic() - _START)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return Stats(
        uptime=f"{h}h:{m:02d}m:{s:02d}s",
        disk=psutil.disk_usage("/").percent,
        cpu=psutil.cpu_percent(interval=None),
        ram=psutil.virtual_memory().percent,
    )
