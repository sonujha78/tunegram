"""Keep data/cache from growing forever: least-recently-used files are deleted first."""
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from tunegram.sources.youtube import CACHE_DIR

load_dotenv()

log = logging.getLogger("tunegram.cache")

CACHE_MAX_MB = int(os.getenv("CACHE_MAX_MB") or "1500")
CHECK_EVERY_SECONDS = 600


def _cleanup(cache_dir: Path, max_bytes: int, protected: set[str]) -> int:
    """Delete oldest files until the cache fits in max_bytes. Returns how many were removed."""
    if not cache_dir.is_dir():
        return 0
    files = [
        f
        for f in cache_dir.iterdir()
        if f.is_file() and f.suffix not in (".part", ".ytdl")
    ]
    total = sum(f.stat().st_size for f in files)
    removed = 0
    for f in sorted(files, key=lambda f: f.stat().st_mtime):  # least recently used first
        if total <= max_bytes:
            break
        if f.name.split(".")[0] in protected:  # video id is the part before the first dot
            continue
        size = f.stat().st_size
        try:
            f.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    return removed


def cleanup_now(max_mb: int = CACHE_MAX_MB, protected=()) -> int:
    return _cleanup(CACHE_DIR, max_mb * 1_000_000, set(protected))


async def cache_janitor(player) -> None:
    """Background task: every few minutes trim the cache, never touching files in use."""
    try:
        while True:
            await asyncio.sleep(CHECK_EVERY_SECONDS)
            try:
                removed = await asyncio.to_thread(
                    _cleanup,
                    CACHE_DIR,
                    CACHE_MAX_MB * 1_000_000,
                    player.active_track_ids(),
                )
                if removed:
                    log.info("Cache cleanup removed %s file(s)", removed)
            except Exception:
                log.exception("cache cleanup failed")
    except asyncio.CancelledError:
        pass
