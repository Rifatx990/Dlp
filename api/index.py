import os
import re
import time
import uuid
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

from flask import Flask, request, jsonify, send_file


# ============================================================
# APP
# ============================================================

app = Flask(__name__)


# ============================================================
# DIRECTORIES
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

# Vercel deployment files
BIN_DIR = BASE_DIR / "bin"

# Writable directory on Vercel
TMP_DIR = Path("/tmp")

DOWNLOAD_DIR = TMP_DIR / "downloads"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

API_TOKEN = os.getenv("API_TOKEN", "").strip()

SOCKS5_PROXY = os.getenv("SOCKS5_PROXY", "").strip()


# ============================================================
# CONSTANTS
# ============================================================

FILE_LIFETIME = 300  # 5 minutes

MAX_VIDEO_HEIGHT = 1080


# ============================================================
# FFmpeg AUTO DETECTION
# ============================================================

def find_binary(name):
    """
    Automatically find a binary.

    Search order:

    1. Bundled Vercel /bin directory
    2. /tmp
    3. System PATH
    """

    candidates = [
        BIN_DIR / name,
        TMP_DIR / name,
    ]

    # Check bundled files
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                try:
                    path.chmod(path.stat().st_mode | 0o111)
                except Exception:
                    pass

                return str(path)
        except Exception:
            pass

    # Check PATH
    system_path = shutil.which(name)

    if system_path:
        return system_path

    return None


def detect_ffmpeg():
    ffmpeg = find_binary("ffmpeg")

    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg was not found. "
            "Make sure bin/ffmpeg exists in the Vercel deployment."
        )

    return ffmpeg


def detect_ffprobe():
    ffprobe = find_binary("ffprobe")

    if not ffprobe:
        raise RuntimeError(
            "ffprobe was not found. "
            "Make sure bin/ffprobe exists in the Vercel deployment."
        )

    return ffprobe


def detect_ffmpeg_directory():
    """
    yt-dlp accepts either the binary path or its directory.

    We return the directory containing ffmpeg/ffprobe.
    """

    ffmpeg = detect_ffmpeg()

    return str(Path(ffmpeg).parent)


# ============================================================
# FILE CLEANUP
# ============================================================

def cleanup_old_files():
    """
    Delete files older than 5 minutes.

    Vercel functions are serverless, so this is performed
    whenever the function is invoked rather than relying on
    a permanent background process.
    """

    now = time.time()

    try:
        for item in DOWNLOAD_DIR.iterdir():

            try:
                age = now - item.stat().st_mtime

                if age > FILE_LIFETIME:

                    if item.is_file():
                        item.unlink(missing_ok=True)

                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)

            except Exception:
                continue

    except Exception:
        pass


# ============================================================
# URL VALIDATION
# ============================================================

def valid_youtube_url(url):
    try:

        parsed = urlparse(url)

        hostname = parsed.hostname

        if not hostname:
            return False

        hostname = hostname.lower()

        allowed_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }

        return hostname in allowed_hosts

    except Exception:
        return False


# ============================================================
# YOUTUBE VIDEO ID
# ============================================================

def extract_video_id(url):

    try:

        parsed = urlparse(url)

        hostname = (parsed.hostname or "").lower()

        if hostname in ("youtu.be", "www.youtu.be"):

            video_id = parsed.path.strip("/")

            return video_id or None

        if "youtube.com" in hostname:

            query = parsed.query

            match = re.search(
                r"(?:^|&)v=([^&]+)",
                query
            )

            if match:
                return match.group(1)

            path = parsed.path

            # Shorts
            match = re.search(
                r"/shorts/([^/?]+)",
                path
            )

            if match:
                return match.group(1)

            # Embed
            match = re.search(
                r"/embed/([^/?]+)",
                path
            )

            if match:
                return match.group(1)

        return None

    except Exception:
        return None


# ============================================================
# SAFE FILENAME
# ============================================================

def safe_filename(filename):

    if not filename:
        filename = "download"

    filename = str(filename)

    filename = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        filename
    )

    filename = filename.strip()

    if len(filename) > 150:
        filename = filename[:150]

    return filename or "download"


# ============================================================
# API AUTHORIZATION
# ============================================================

def authorized_request():

    # If API_TOKEN is not configured, allow request.
    if not API_TOKEN:
        return True

    provided_token = request.headers.get(
        "X-API-Token",
        ""
    )

    return provided_token == API_TOKEN


# ============================================================
# PROXY
# ============================================================

def get_proxy():

    if not SOCKS5_PROXY:
        return None

    return SOCKS5_PROXY


# ============================================================
# ROOT
# ============================================================

@app.route("/", methods=["GET"])
def index():

    return jsonify({
        "name": "YouTube Downloader API",
        "status": "online",
        "runtime": "Vercel",
        "ffmpeg": detect_ffmpeg() if find_binary("ffmpeg") else None,
        "ffprobe": detect_ffprobe() if find_binary("ffprobe") else None,
        "endpoints": {
            "health": "/healthz",
            "search": "/search",
            "download": "/download"
        }
    })


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "status": "ok"
    })


@app.route("/healthz", methods=["GET"])
def healthz():

    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")

    return jsonify({
        "status": "ok",
        "ffmpeg_found": bool(ffmpeg),
        "ffprobe_found": bool(ffprobe),
        "ffmpeg_path": ffmpeg,
        "ffprobe_path": ffprobe,
        "download_directory": str(DOWNLOAD_DIR)
    })


# ============================================================
# YOUTUBE SEARCH
# ============================================================

@app.route("/search", methods=["GET"])
def search():

    if not authorized_request():

        return jsonify({
            "error": "Unauthorized"
        }), 401

    query = request.args.get(
        "q",
        ""
    ).strip()

    if not query:

        return jsonify({
            "error": "Missing search query"
        }), 400

    if not YOUTUBE_API_KEY:

        return jsonify({
            "error": "YOUTUBE_API_KEY is not configured"
        }), 500

    try:

        response = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": 10,
                "key": YOUTUBE_API_KEY
            },
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        results = []

        for item in data.get("items", []):

            video_id = (
                item.get("id", {})
                .get("videoId")
            )

            snippet = item.get(
                "snippet",
                {}
            )

            if not video_id:
                continue

            results.append({
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": snippet.get(
                    "title",
                    ""
                ),
                "description": snippet.get(
                    "description",
                    ""
                ),
                "channel": snippet.get(
                    "channelTitle",
                    ""
                ),
                "thumbnail": (
                    snippet
                    .get("thumbnails", {})
                    .get("high", {})
                    .get("url")
                ),
                "published_at": snippet.get(
                    "publishedAt"
                )
            })

        return jsonify({
            "query": query,
            "results": results
        })

    except requests.RequestException as e:

        return jsonify({
            "error": "YouTube API request failed",
            "details": str(e)
        }), 502

    except Exception as e:

        return jsonify({
            "error": "Search failed",
            "details": str(e)
        }), 500


# ============================================================
# DOWNLOAD
# ============================================================

@app.route("/download", methods=["GET", "POST"])
def download():

    if not authorized_request():

        return jsonify({
            "error": "Unauthorized"
        }), 401

    # Cleanup old files
    cleanup_old_files()

    # --------------------------------------------------------
    # Get request parameters
    # --------------------------------------------------------

    if request.method == "POST":

        data = request.get_json(
            silent=True
        ) or {}

        url = str(
            data.get("url", "")
        ).strip()

        download_type = str(
            data.get("type", "video")
        ).lower().strip()

    else:

        url = request.args.get(
            "url",
            ""
        ).strip()

        download_type = request.args.get(
            "type",
            "video"
        ).lower().strip()

    # --------------------------------------------------------
    # Validate URL
    # --------------------------------------------------------

    if not url:

        return jsonify({
            "error": "Missing YouTube URL"
        }), 400

    if not valid_youtube_url(url):

        return jsonify({
            "error": "Invalid YouTube URL"
        }), 400

    # --------------------------------------------------------
    # Validate type
    # --------------------------------------------------------

    if download_type not in (
        "video",
        "audio"
    ):

        return jsonify({
            "error": "type must be 'video' or 'audio'"
        }), 400

    # --------------------------------------------------------
    # Detect FFmpeg
    # --------------------------------------------------------

    try:

        ffmpeg_path = detect_ffmpeg()
        ffprobe_path = detect_ffprobe()

        ffmpeg_dir = str(
            Path(ffmpeg_path).parent
        )

    except Exception as e:

        return jsonify({
            "error": "FFmpeg configuration error",
            "details": str(e)
        }), 500

    # --------------------------------------------------------
    # Unique job directory
    # --------------------------------------------------------

    job_id = uuid.uuid4().hex

    job_dir = DOWNLOAD_DIR / job_id

    job_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # --------------------------------------------------------
    # Output template
    # --------------------------------------------------------

    output_template = str(
        job_dir / "%(title).150B.%(ext)s"
    )

    # --------------------------------------------------------
    # yt-dlp configuration
    # --------------------------------------------------------

    ydl_opts = {

        "outtmpl": output_template,

        "noplaylist": True,

        "nocheckcertificate": True,

        "retries": 3,

        "fragment_retries": 3,

        "socket_timeout": 30,

        "ffmpeg_location": ffmpeg_dir,

        "quiet": True,

        "no_warnings": False,

        "overwrites": True,

        "continuedl": False,

        "paths": {
            "home": str(job_dir),
            "temp": str(job_dir / "temp")
        },

        "http_headers": {
            "User-Agent":
                "Mozilla/5.0 "
                "(Linux; Android 15) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Mobile Safari/537.36"
        }
    }

    # --------------------------------------------------------
    # Proxy
    # --------------------------------------------------------

    proxy = get_proxy()

    if proxy:

        ydl_opts["proxy"] = proxy

    # --------------------------------------------------------
    # Video configuration
    # --------------------------------------------------------

    if download_type == "video":

        ydl_opts.update({

            "format":
                "bestvideo[height<=1080]+"
                "bestaudio/"
                "best[height<=1080]",

            "merge_output_format":
                "mp4"
        })

    # --------------------------------------------------------
    # Audio configuration
    # --------------------------------------------------------

    elif download_type == "audio":

        ydl_opts.update({

            "format":
                "bestaudio/best",

            "postprocessors": [

                {
                    "key":
                        "FFmpegExtractAudio",

                    "preferredcodec":
                        "mp3",

                    "preferredquality":
                        "192"
                }

            ]
        })

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

        # ----------------------------------------------------
        # Find resulting file
        # ----------------------------------------------------

        files = []

        for file in job_dir.rglob("*"):

            if not file.is_file():
                continue

            # Ignore temporary files
            if file.name.endswith(
                (".part", ".ytdl")
            ):
                continue

            files.append(file)

        if not files:

            raise RuntimeError(
                "Download completed but no output file was found."
            )

        # Usually the newest file is the final output
        output_file = max(
            files,
            key=lambda p: p.stat().st_mtime
        )

        # ----------------------------------------------------
        # Filename
        # ----------------------------------------------------

        filename = safe_filename(
            output_file.name
        )

        # ----------------------------------------------------
        # Return response
        # ----------------------------------------------------

        return send_file(

            output_file,

            as_attachment=True,

            download_name=filename,

            mimetype=(
                "audio/mpeg"
                if download_type == "audio"
                else "video/mp4"
            )
        )

    except yt_dlp.DownloadError as e:

        shutil.rmtree(
            job_dir,
            ignore_errors=True
        )

        return jsonify({

            "error":
                "The video could not be downloaded.",

            "details":
                str(e),

            "ffmpeg":
                ffmpeg_path,

            "ffprobe":
                ffprobe_path

        }), 500

    except Exception as e:

        shutil.rmtree(
            job_dir,
            ignore_errors=True
        )

        return jsonify({

            "error":
                "Download failed.",

            "details":
                str(e),

            "ffmpeg":
                ffmpeg_path,

            "ffprobe":
                ffprobe_path

        }), 500


# ============================================================
# VERCEL HANDLER
# ============================================================

# Do NOT use app.run() on Vercel.
