"""Find which YouTube client gives an audio URL that ffmpeg can stream directly."""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv

load_dotenv()

VIDEO = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=Umqb9KENgmk"
CLIENTS = ["default", "tv", "web_safari", "mweb", "web_embedded", "android_vr", "ios"]


def base_opts(client: str, cookies: bool) -> dict:
    o = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": "bestaudio/best",
    }
    deno = shutil.which("deno") or str(Path.home() / ".deno" / "bin" / "deno")
    if os.path.isfile(deno):
        o["js_runtimes"] = {"deno": {"path": deno}}
    if client != "default":
        o["extractor_args"] = {"youtube": {"player_client": [client]}}
    cookie_file = os.getenv("YT_COOKIES_FILE")
    if cookies and cookie_file:
        o["cookiefile"] = cookie_file
    return o


def try_ffmpeg(info: dict) -> str:
    headers = dict(info.get("http_headers") or {})
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    ua = headers.pop("User-Agent", None)
    if ua:
        cmd += ["-user_agent", ua]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd += ["-i", info["url"], "-t", "5", "-f", "null", "-"]
    started = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    took = time.perf_counter() - started
    if r.returncode == 0:
        return f"OK ({took:.1f}s to read 5s of audio)"
    err = re.sub(r"https?://\S+", "<url>", r.stderr).strip().splitlines()
    return "FAIL: " + (err[-1][:80] if err else f"exit {r.returncode}")


for client in CLIENTS:
    for cookies in (True, False):
        label = f"{client:13} cookies={'yes' if cookies else 'no '}"
        t0 = time.perf_counter()
        try:
            with yt_dlp.YoutubeDL(base_opts(client, cookies)) as ydl:
                info = ydl.extract_info(VIDEO, download=False)
        except Exception as exc:
            print(f"{label} | extract FAILED: {str(exc)[:70]}", flush=True)
            continue
        extract = time.perf_counter() - t0
        fmt = (
            f"{info.get('format_id')} {info.get('ext')} "
            f"{info.get('protocol')} {int(info.get('abr') or 0)}k"
        )
        print(f"{label} | extract {extract:.1f}s | {fmt} | ffmpeg {try_ffmpeg(info)}", flush=True)
