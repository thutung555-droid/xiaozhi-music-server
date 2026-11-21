import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import yt_dlp

app = FastAPI(title="Xiaozhi Music Server", version="1.0.0")

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/xiaozhi_music_cache"))
if os.name == "nt":
    CACHE_DIR = Path(os.environ.get("CACHE_DIR", str(Path.home() / ".xiaozhi_music_cache")))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# In-memory index: track_id -> metadata
TRACKS: Dict[str, dict] = {}


def find_ffmpeg() -> str:
    env_ffmpeg = os.environ.get("FFMPEG_PATH")
    if env_ffmpeg and Path(env_ffmpeg).exists():
        return env_ffmpeg
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


FFMPEG_BIN = find_ffmpeg()


def _extract(prefix: str, query: str, extra_opts: Optional[dict] = None) -> Optional[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "noplaylist": True,
        "format": "bestaudio/best",
    }
    if extra_opts:
        ydl_opts.update(extra_opts)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"{prefix}{query}", download=False)
        if not info:
            return None
        entries = info.get("entries")
        if entries:
            info = entries[0]
        if not info or not info.get("url"):
            return None
        return {
            "id": str(info.get("id") or hashlib.md5(query.encode()).hexdigest()[:12]),
            "title": info.get("title") or query,
            "artist": info.get("uploader") or info.get("channel") or "Music",
            "duration": int(info.get("duration") or 0),
            "webpage_url": info.get("webpage_url") or "",
            "stream_url": info.get("url"),
        }


def search_youtube(query: str) -> Optional[dict]:
    # 1. Try YouTube with android/tv client first
    try:
        res = _extract(
            "ytsearch1:",
            query,
            {"extractor_args": {"youtube": {"player_client": ["android", "tv"]}}},
        )
        if res:
            return res
    except Exception as e:
        print(f"[ytsearch] failed: {e}")

    # 2. Fallback to SoundCloud (works reliably on cloud/datacenter IPs)
    try:
        res = _extract("scsearch1:", query)
        if res:
            return res
    except Exception as e:
        print(f"[scsearch] failed: {e}")

    return None


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Xiaozhi Music Server (YouTube -> MP3)",
        "ffmpeg": FFMPEG_BIN,
        "cached_tracks": len(TRACKS),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stream_pcm")
def stream_pcm(
    song: str = Query(..., description="Song name"),
    artist: str = Query("", description="Artist name"),
):
    query = f"{song} {artist}".strip()
    if not query:
        raise HTTPException(status_code=400, detail="Missing song parameter")

    track = search_youtube(query)
    if not track or not track.get("stream_url"):
        raise HTTPException(status_code=404, detail=f"Song not found: {query}")

    track_id = track["id"]
    TRACKS[track_id] = track

    return JSONResponse(
        {
            "title": track["title"],
            "artist": track["artist"],
            "duration": track["duration"],
            "audio_url": f"/audio/{track_id}.mp3",
            "lyric_url": "",
        }
    )


@app.get("/audio/{track_id}.mp3")
def get_audio(track_id: str):
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    cached_mp3 = CACHE_DIR / f"{track_id}.mp3"
    if cached_mp3.exists() and cached_mp3.stat().st_size > 16384:
        return FileResponse(cached_mp3, media_type="audio/mpeg")

    stream_url = track.get("stream_url")
    if not stream_url:
        refreshed = search_youtube(track.get("webpage_url") or track["title"])
        if not refreshed or not refreshed.get("stream_url"):
            raise HTTPException(status_code=502, detail="Failed to resolve audio stream")
        stream_url = refreshed["stream_url"]

    # Transcode to 128kbps 44.1kHz stereo/mono MP3 compatible with ESP32-S3 MP3 decoder
    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "3",
        "-i",
        stream_url,
        "-vn",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-f",
        "mp3",
        "pipe:1",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def iter_stream():
        tmp_path = CACHE_DIR / f"{track_id}.tmp"
        total = 0
        try:
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    yield chunk
            proc.wait(timeout=5)
            if proc.returncode == 0 and total > 16384:
                tmp_path.replace(cached_mp3)
            elif tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        except Exception:
            proc.kill()
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    return StreamingResponse(iter_stream(), media_type="audio/mpeg")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
