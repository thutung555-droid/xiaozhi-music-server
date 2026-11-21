import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import yt_dlp
from ytmusicapi import YTMusic

app = FastAPI(title="Xiaozhi Music Server", version="1.1.0")

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/xiaozhi_music_cache"))
if os.name == "nt":
    CACHE_DIR = Path(os.environ.get("CACHE_DIR", str(Path.home() / ".xiaozhi_music_cache")))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_PATH = CACHE_DIR / "cookies.txt"

# In-memory index: track_id -> metadata
TRACKS: Dict[str, dict] = {}


def get_cookie_file() -> Optional[str]:
    cookies_env = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies_env:
        # Handle escaped newlines if pasted as a single line
        if "\\n" in cookies_env and "\n" not in cookies_env:
            cookies_env = cookies_env.replace("\\n", "\n")
        COOKIE_PATH.write_text(cookies_env + "\n", encoding="utf-8")
        return str(COOKIE_PATH)
    if COOKIE_PATH.exists() and COOKIE_PATH.stat().st_size > 0:
        return str(COOKIE_PATH)
    return None


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
YTM = YTMusic()


def _extract(target: str, extra_opts: Optional[dict] = None) -> Optional[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "noplaylist": True,
        "format": "bestaudio/best/18",
    }
    cookie_file = get_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    if extra_opts:
        ydl_opts.update(extra_opts)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if not info:
            return None
        entries = info.get("entries")
        if entries:
            info = entries[0]
        if not info or not info.get("url"):
            return None
        artists = info.get("artists")
        artist_name = (
            ", ".join(artists)
            if isinstance(artists, list) and artists
            else (info.get("uploader") or info.get("channel") or "Music")
        )
        return {
            "id": str(info.get("id") or hashlib.md5(target.encode()).hexdigest()[:12]),
            "title": info.get("title") or "Unknown",
            "artist": artist_name,
            "duration": int(info.get("duration") or 0),
            "webpage_url": info.get("webpage_url") or "",
            "stream_url": info.get("url"),
        }


def search_music(query: str) -> Optional[dict]:
    # 1. Try YouTube Music first (clean audio tracks, no MV intros)
    try:
        results = YTM.search(query, filter="songs", limit=1)
        if not results:
            results = YTM.search(query, filter="videos", limit=1)
        if results and results[0].get("videoId"):
            vid = results[0]["videoId"]
            res = _extract(f"https://music.youtube.com/watch?v={vid}")
            if res:
                if results[0].get("title"):
                    res["title"] = results[0]["title"]
                ytm_artists = results[0].get("artists")
                if isinstance(ytm_artists, list) and ytm_artists:
                    res["artist"] = ", ".join(a.get("name", "") for a in ytm_artists if a.get("name"))
                res["source"] = "ytmusic"
                return res
    except Exception as e:
        print(f"[ytmusic] failed: {e}")

    # 2. Try YouTube search
    try:
        res = _extract(f"ytsearch1:{query}")
        if res:
            res["source"] = "youtube"
            return res
    except Exception as e:
        print(f"[ytsearch] failed: {e}")

    # 3. Fallback to SoundCloud (works reliably without cookies)
    try:
        res = _extract(f"scsearch1:{query}")
        if res:
            res["source"] = "soundcloud"
            return res
    except Exception as e:
        print(f"[scsearch] failed: {e}")

    return None


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Xiaozhi Music Server (YouTube Music + SoundCloud -> MP3)",
        "ffmpeg": FFMPEG_BIN,
        "cookies_loaded": bool(get_cookie_file()),
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

    track = search_music(query)
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
            "source": track.get("source", "unknown"),
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
        refreshed = search_music(track.get("webpage_url") or track["title"])
        if not refreshed or not refreshed.get("stream_url"):
            raise HTTPException(status_code=502, detail="Failed to resolve audio stream")
        stream_url = refreshed["stream_url"]

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
