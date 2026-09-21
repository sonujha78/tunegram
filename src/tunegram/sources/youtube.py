"""YouTube search + audio stream URL resolving (yt-dlp)."""
import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from yt_dlp.utils import DownloadError

load_dotenv()


class SourceError(Exception):
    """Search ya resolve fail hone pe."""


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    uploader: str
    duration: int  # seconds
    webpage_url: str
    thumbnail: str | None = None
    stream_url: str | None = None  # direct audio URL (resolve() se bharta hai)
    headers: dict | None = None  # stream kholne ke liye zaruri HTTP headers (User-Agent etc.)
    requested_by: str = ""  # who asked for this track (shown in the panel)


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    deno = shutil.which("deno") or str(Path.home() / ".deno" / "bin" / "deno")
    if os.path.isfile(deno):
        # yt-dlp ko YouTube ke liye JS runtime chahiye; explicit path taaki PATH pe depend na ho
        opts["js_runtimes"] = {"deno": {"path": deno}}
    cookies = os.getenv("YT_COOKIES_FILE")
    if cookies:
        if not os.path.isfile(cookies):
            raise SourceError(f"YT_COOKIES_FILE is set but the file was not found: {cookies}")
        opts["cookiefile"] = cookies
    return opts


def format_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


def _search_sync(query: str, limit: int) -> list[Track]:
    opts = {**_base_opts(), "extract_flat": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    except DownloadError as exc:
        raise SourceError(f"Search failed: {exc}") from exc

    tracks: list[Track] = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        thumbs = e.get("thumbnails") or []
        tracks.append(
            Track(
                id=e["id"],
                title=e.get("title") or "Unknown",
                uploader=e.get("channel") or e.get("uploader") or "Unknown",
                duration=int(e.get("duration") or 0),
                webpage_url=f"https://www.youtube.com/watch?v={e['id']}",
                thumbnail=thumbs[-1].get("url") if thumbs else None,
            )
        )
    return tracks


def _resolve_sync(url: str) -> Track:
    opts = {**_base_opts(), "format": "bestaudio/best"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise SourceError(f"Could not get audio: {exc}") from exc

    if not info or not info.get("url"):
        raise SourceError("No playable audio stream found")
    return Track(
        id=info["id"],
        title=info.get("title") or "Unknown",
        uploader=info.get("channel") or info.get("uploader") or "Unknown",
        duration=int(info.get("duration") or 0),
        webpage_url=info.get("webpage_url") or url,
        thumbnail=info.get("thumbnail"),
        stream_url=info["url"],
        headers=dict(info.get("http_headers") or {}),
    )


def ffmpeg_header_args(track: Track) -> list[str]:
    """ffmpeg/ffplay input options: stream URL ke saath wahi headers bhejo jo yt-dlp ne use kiye."""
    h = dict(track.headers or {})
    ua = h.pop("User-Agent", None)
    args: list[str] = []
    if ua:
        args += ["-user_agent", ua]
    if h:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in h.items())]
    return args


async def search(query: str, limit: int = 5) -> list[Track]:
    return await asyncio.to_thread(_search_sync, query, limit)


async def resolve(track: Track) -> Track:
    """Direct audio URL nikalo. URL kuch ghante me expire hota hai,
    isliye play hone se theek pehle resolve karo, pehle se store mat karo."""
    resolved = await asyncio.to_thread(_resolve_sync, track.webpage_url)
    return replace(resolved, thumbnail=resolved.thumbnail or track.thumbnail)


async def get_track(query: str) -> Track:
    """Text ho ya YouTube URL, ek playable Track wapas do."""
    if is_url(query):
        return await asyncio.to_thread(_resolve_sync, query.strip())
    results = await search(query, limit=1)
    if not results:
        raise SourceError("No results found")
    return await resolve(results[0])


CACHE_DIR = Path("data/cache")
AUDIO_FORMAT = "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio"


def _download_sync(track: Track) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for f in CACHE_DIR.glob(f"{track.id}.*"):
        if f.suffix not in (".part", ".ytdl"):
            return f  # pehle se cache me hai

    opts = {
        **_base_opts(),
        "skip_download": False,
        "format": AUDIO_FORMAT,
        "outtmpl": str(CACHE_DIR / "%(id)s.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track.webpage_url, download=True)
            path = Path(ydl.prepare_filename(info))
    except DownloadError as exc:
        raise SourceError(f"Download failed: {exc}") from exc

    if not path.is_file():
        raise SourceError("Download finished but file not found")
    return path


async def download(track: Track) -> Path:
    """Audio ko data/cache me download karo aur local file ka path do."""
    return await asyncio.to_thread(_download_sync, track)


async def _cli(query: str, play: bool) -> None:
    print(f"Searching: {query!r}\n")
    results = await search(query, limit=5)
    for i, t in enumerate(results, 1):
        print(f"{i}. {t.title} | {t.uploader} | {format_duration(t.duration)}")

    if is_url(query):
        track = await get_track(query)
    elif results:
        track = results[0]
    else:
        raise SourceError("No results found")

    print(f"\nSelected: {track.title} ({format_duration(track.duration)})")
    print("Downloading audio to cache...")
    path = await download(track)
    print(f"Saved: {path} ({path.stat().st_size / 1_000_000:.1f} MB)")
    if play:
        print("Playing 20 seconds with ffplay...")
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-t", "20", "-loglevel", "error", str(path)]
        )

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--play"]
    if not args:
        sys.exit('Usage: python -m tunegram.sources.youtube "song name" [--play]')
    try:
        asyncio.run(_cli(" ".join(args), play="--play" in sys.argv))
    except SourceError as exc:
        sys.exit(f"\nFAILED: {str(exc)[:300]}")
