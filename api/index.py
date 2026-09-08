import os
import re
import time
import uuid
import shutil
import subprocess
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
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

# Bundled binaries:
# /var/task/bin/ffmpeg
# /var/task/bin/ffprobe
BIN_DIR = BASE_DIR / "bin"

# Vercel writable directory
TMP_DIR = Path("/tmp")

DOWNLOAD_DIR = TMP_DIR / "downloads"

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

YOUTUBE_API_KEY = os.getenv(
    "YOUTUBE_API_KEY",
    ""
).strip()

API_TOKEN = os.getenv(
    "API_TOKEN",
    ""
).strip()

SOCKS5_PROXY = os.getenv(
    "SOCKS5_PROXY",
    ""
).strip()


# ============================================================
# SETTINGS
# ============================================================

FILE_LIFETIME = 300  # 5 minutes

MAX_VIDEO_HEIGHT = 1080


# ============================================================
# BINARY DETECTION
# ============================================================

def find_binary(name):
    """
    Find FFmpeg/FFprobe automatically.

    Search order:

    1. /var/task/bin/
    2. /tmp/
    3. system PATH
    """

    candidates = [
        BIN_DIR / name,
        TMP_DIR / name
    ]

    # --------------------------------------------------------
    # Bundled binary
    # --------------------------------------------------------

    for path in candidates:

        try:

            if path.exists() and path.is_file():

                # Try to make executable
                try:

                    current_mode = path.stat().st_mode

                    path.chmod(
                        current_mode | 0o111
                    )

                except Exception:
                    pass

                return str(path)

        except Exception:
            continue

    # --------------------------------------------------------
    # System binary
    # --------------------------------------------------------

    system_path = shutil.which(name)

    if system_path:
        return system_path

    return None


# ============================================================
# EXECUTE BINARY
# ============================================================

def test_binary(path):
    """
    Check whether a binary can actually execute.

    Merely finding the file is not enough.
    """

    if not path:

        return {
            "found": False,
            "executable": False,
            "error": "Binary not found"
        }

    try:

        # Make executable
        try:

            p = Path(path)

            p.chmod(
                p.stat().st_mode | 0o111
            )

        except Exception:
            pass

        result = subprocess.run(

            [
                path,
                "-version"
            ],

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            text=True,

            timeout=10
        )

        output = (
            result.stdout
            if result.stdout
            else result.stderr
        )

        version = None

        if output:

            version = output.splitlines()[0]

        return {

            "found": True,

            "executable":
                result.returncode == 0,

            "return_code":
                result.returncode,

            "version":
                version

        }

    except PermissionError as e:

        return {

            "found": True,

            "executable": False,

            "error":
                "Permission denied: " + str(e)

        }

    except OSError as e:

        return {

            "found": True,

            "executable": False,

            "error":
                "OS execution error: " + str(e)

        }

    except subprocess.TimeoutExpired:

        return {

            "found": True,

            "executable": False,

            "error":
                "Binary execution timed out"

        }

    except Exception as e:

        return {

            "found": True,

            "executable": False,

            "error":
                repr(e)

        }


# ============================================================
# DETECT FFMPEG
# ============================================================

def detect_ffmpeg():

    path = find_binary(
        "ffmpeg"
    )

    if not path:

        raise RuntimeError(
            "FFmpeg was not found. "
            "Make sure bin/ffmpeg exists."
        )

    result = test_binary(path)

    if not result.get(
        "executable",
        False
    ):

        raise RuntimeError(
            "FFmpeg was found but cannot "
            "be executed: "
            + str(result)
        )

    return path


# ============================================================
# DETECT FFPROBE
# ============================================================

def detect_ffprobe():

    path = find_binary(
        "ffprobe"
    )

    if not path:

        raise RuntimeError(
            "ffprobe was not found. "
            "Make sure bin/ffprobe exists."
        )

    result = test_binary(path)

    if not result.get(
        "executable",
        False
    ):

        raise RuntimeError(
            "ffprobe was found but cannot "
            "be executed: "
            + str(result)
        )

    return path


# ============================================================
# FFMPEG DIRECTORY
# ============================================================

def detect_ffmpeg_directory():

    ffmpeg = detect_ffmpeg()
    ffprobe = detect_ffprobe()

    ffmpeg_path = Path(
        ffmpeg
    )

    ffprobe_path = Path(
        ffprobe
    )

    # Both binaries should normally be
    # inside the same directory.

    if ffmpeg_path.parent != ffprobe_path.parent:

        # yt-dlp can still accept the ffmpeg
        # binary path directly.
        return ffmpeg

    return str(
        ffmpeg_path.parent
    )


# ============================================================
# CLEANUP
# ============================================================

def cleanup_old_files():

    now = time.time()

    try:

        if not DOWNLOAD_DIR.exists():
            return

        for item in DOWNLOAD_DIR.iterdir():

            try:

                age = (
                    now -
                    item.stat().st_mtime
                )

                if age > FILE_LIFETIME:

                    if item.is_file():

                        item.unlink(
                            missing_ok=True
                        )

                    elif item.is_dir():

                        shutil.rmtree(
                            item,
                            ignore_errors=True
                        )

            except Exception:
                continue

    except Exception:
        pass


# ============================================================
# YOUTUBE URL VALIDATION
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
            "www.youtu.be"

        }

        return hostname in allowed_hosts

    except Exception:

        return False


# ============================================================
# VIDEO ID
# ============================================================

def extract_video_id(url):

    try:

        parsed = urlparse(url)

        hostname = (
            parsed.hostname or ""
        ).lower()

        # youtu.be
        if hostname in (
            "youtu.be",
            "www.youtu.be"
        ):

            video_id = (
                parsed.path
                .strip("/")
            )

            return video_id or None

        # youtube.com
        if "youtube.com" in hostname:

            # ?v=
            match = re.search(
                r"(?:^|&)v=([^&]+)",
                parsed.query
            )

            if match:

                return match.group(1)

            # /shorts/ID
            match = re.search(
                r"/shorts/([^/?]+)",
                parsed.path
            )

            if match:

                return match.group(1)

            # /embed/ID
            match = re.search(
                r"/embed/([^/?]+)",
                parsed.path
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

    filename = str(
        filename
    )

    filename = re.sub(

        r'[<>:"/\\|?*\x00-\x1F]',

        "_",

        filename
    )

    filename = filename.strip()

    if len(filename) > 150:

        filename = filename[:150]

    return (
        filename
        or "download"
    )


# ============================================================
# AUTHORIZATION
# ============================================================

def authorized_request():

    # If API_TOKEN is empty,
    # authentication is disabled.

    if not API_TOKEN:

        return True

    provided_token = request.headers.get(
        "X-API-Token",
        ""
    )

    return (
        provided_token ==
        API_TOKEN
    )


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

@app.route(
    "/",
    methods=["GET"]
)
def index():

    ffmpeg = find_binary(
        "ffmpeg"
    )

    ffprobe = find_binary(
        "ffprobe"
    )

    return jsonify({

        "name":
            "YouTube Downloader API",

        "status":
            "online",

        "runtime":
            "Vercel",

        "ffmpeg":
            ffmpeg,

        "ffprobe":
            ffprobe,

        "endpoints": {

            "health":
                "/health",

            "healthz":
                "/healthz",

            "search":
                "/search",

            "download":
                "/download"

        }

    })


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify({

        "status":
            "ok"

    })


# ============================================================
# HEALTHZ
# ============================================================

@app.route(
    "/healthz",
    methods=["GET"]
)
def healthz():

    ffmpeg = find_binary(
        "ffmpeg"
    )

    ffprobe = find_binary(
        "ffprobe"
    )

    ffmpeg_test = test_binary(
        ffmpeg
    )

    ffprobe_test = test_binary(
        ffprobe
    )

    return jsonify({

        "status":
            "ok",

        "ffmpeg":
            {
                "found":
                    bool(ffmpeg),

                "path":
                    ffmpeg,

                **ffmpeg_test
            },

        "ffprobe":
            {
                "found":
                    bool(ffprobe),

                "path":
                    ffprobe,

                **ffprobe_test
            },

        "download_directory":
            str(DOWNLOAD_DIR)

    })


# ============================================================
# SEARCH
# ============================================================

@app.route(
    "/search",
    methods=["GET"]
)
def search():

    if not authorized_request():

        return jsonify({

            "error":
                "Unauthorized"

        }), 401

    query = request.args.get(
        "q",
        ""
    ).strip()

    if not query:

        return jsonify({

            "error":
                "Missing search query"

        }), 400

    if not YOUTUBE_API_KEY:

        return jsonify({

            "error":
                "YOUTUBE_API_KEY is not configured"

        }), 500

    try:

        response = requests.get(

            "https://www.googleapis.com/"
            "youtube/v3/search",

            params={

                "part":
                    "snippet",

                "q":
                    query,

                "type":
                    "video",

                "maxResults":
                    10,

                "key":
                    YOUTUBE_API_KEY

            },

            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        results = []

        for item in data.get(
            "items",
            []
        ):

            video_id = (
                item
                .get("id", {})
                .get("videoId")
            )

            snippet = item.get(
                "snippet",
                {}
            )

            if not video_id:
                continue

            thumbnails = snippet.get(
                "thumbnails",
                {}
            )

            thumbnail = (
                thumbnails
                .get("high", {})
                .get("url")
            )

            if not thumbnail:

                thumbnail = (
                    thumbnails
                    .get("medium", {})
                    .get("url")
                )

            if not thumbnail:

                thumbnail = (
                    thumbnails
                    .get("default", {})
                    .get("url")
                )

            results.append({

                "video_id":
                    video_id,

                "url":
                    "https://www.youtube.com/watch?v="
                    + video_id,

                "title":
                    snippet.get(
                        "title",
                        ""
                    ),

                "description":
                    snippet.get(
                        "description",
                        ""
                    ),

                "channel":
                    snippet.get(
                        "channelTitle",
                        ""
                    ),

                "thumbnail":
                    thumbnail,

                "published_at":
                    snippet.get(
                        "publishedAt"
                    )

            })

        return jsonify({

            "query":
                query,

            "results":
                results

        })

    except requests.RequestException as e:

        return jsonify({

            "error":
                "YouTube API request failed",

            "details":
                str(e)

        }), 502

    except Exception as e:

        return jsonify({

            "error":
                "Search failed",

            "details":
                str(e)

        }), 500


# ============================================================
# DOWNLOAD
# ============================================================

@app.route(
    "/download",
    methods=["GET", "POST"]
)
def download():

    # --------------------------------------------------------
    # AUTH
    # --------------------------------------------------------

    if not authorized_request():

        return jsonify({

            "error":
                "Unauthorized"

        }), 401

    # --------------------------------------------------------
    # CLEAN OLD FILES
    # --------------------------------------------------------

    cleanup_old_files()

    # --------------------------------------------------------
    # GET PARAMETERS
    # --------------------------------------------------------

    if request.method == "POST":

        data = request.get_json(
            silent=True
        ) or {}

        url = str(
            data.get(
                "url",
                ""
            )
        ).strip()

        download_type = str(
            data.get(
                "type",
                "video"
            )
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
    # URL VALIDATION
    # --------------------------------------------------------

    if not url:

        return jsonify({

            "error":
                "Missing YouTube URL"

        }), 400

    if not valid_youtube_url(url):

        return jsonify({

            "error":
                "Invalid YouTube URL"

        }), 400

    # --------------------------------------------------------
    # TYPE VALIDATION
    # --------------------------------------------------------

    if download_type not in (
        "video",
        "audio"
    ):

        return jsonify({

            "error":
                "type must be 'video' or 'audio'"

        }), 400

    # --------------------------------------------------------
    # FFMPEG DETECTION
    # --------------------------------------------------------

    try:

        ffmpeg_path = detect_ffmpeg()

        ffprobe_path = detect_ffprobe()

        ffmpeg_location = (
            detect_ffmpeg_directory()
        )

    except Exception as e:

        return jsonify({

            "error":
                "FFmpeg configuration error",

            "details":
                str(e),

            "ffmpeg":
                find_binary("ffmpeg"),

            "ffprobe":
                find_binary("ffprobe")

        }), 500

    # --------------------------------------------------------
    # JOB DIRECTORY
    # --------------------------------------------------------

    job_id = uuid.uuid4().hex

    job_dir = (
        DOWNLOAD_DIR /
        job_id
    )

    temp_dir = (
        job_dir /
        "temp"
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    temp_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # --------------------------------------------------------
    # OUTPUT TEMPLATE
    # --------------------------------------------------------

    output_template = str(

        job_dir /
        "%(title).150B.%(ext)s"

    )

    # --------------------------------------------------------
    # YT-DLP OPTIONS
    # --------------------------------------------------------

    ydl_opts = {

        "outtmpl":
            output_template,

        "noplaylist":
            True,

        "nocheckcertificate":
            True,

        "retries":
            3,

        "fragment_retries":
            3,

        "socket_timeout":
            30,

        "ffmpeg_location":
            ffmpeg_location,

        "quiet":
            True,

        "no_warnings":
            False,

        "overwrites":
            True,

        "continuedl":
            False,

        "paths": {

            "home":
                str(job_dir),

            "temp":
                str(temp_dir)

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
    # PROXY
    # --------------------------------------------------------

    proxy = get_proxy()

    if proxy:

        ydl_opts["proxy"] = proxy

    # --------------------------------------------------------
    # VIDEO
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
    # AUDIO
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
    # DOWNLOAD
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
        # FIND OUTPUT
        # ----------------------------------------------------

        files = []

        for file in job_dir.rglob("*"):

            if not file.is_file():
                continue

            if file.name.endswith(
                (
                    ".part",
                    ".ytdl",
                    ".tmp"
                )
            ):
                continue

            files.append(file)

        if not files:

            raise RuntimeError(
                "Download completed but "
                "no output file was found."
            )

        # Newest file
        output_file = max(

            files,

            key=lambda p:
                p.stat().st_mtime

        )

        # ----------------------------------------------------
        # FILENAME
        # ----------------------------------------------------

        filename = safe_filename(
            output_file.name
        )

        # ----------------------------------------------------
        # MIME TYPE
        # ----------------------------------------------------

        if download_type == "audio":

            mimetype = "audio/mpeg"

        else:

            mimetype = "video/mp4"

        # ----------------------------------------------------
        # SEND FILE
        # ----------------------------------------------------

        return send_file(

            output_file,

            as_attachment=True,

            download_name=filename,

            mimetype=mimetype

        )

    # --------------------------------------------------------
    # YT-DLP ERROR
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # GENERAL ERROR
    # --------------------------------------------------------

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

    finally:

        # We intentionally don't delete the successful
        # output immediately because send_file needs it.

        pass


# ============================================================
# VERCEL
# ============================================================

# DO NOT use app.run() here.
#
# Vercel automatically loads the Flask "app" object.
