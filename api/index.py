import os
import re
import uuid
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

from flask import Flask, request, jsonify, send_file


app = Flask(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

YOUTUBE_API_KEY = os.environ.get(
    "YOUTUBE_API_KEY",
    ""
).strip()

API_TOKEN = os.environ.get(
    "API_TOKEN",
    ""
).strip()

SOCKS5_PROXY = os.environ.get(
    "SOCKS5_PROXY",
    ""
).strip()


# Vercel filesystem is temporary.
BASE_DIR = Path("/tmp")
DOWNLOAD_DIR = BASE_DIR / "downloads"

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)

FILE_MAX_AGE = 300


# ============================================================
# PROXY
# ============================================================

def get_proxy():

    if not SOCKS5_PROXY:
        return None

    return {
        "http": SOCKS5_PROXY,
        "https": SOCKS5_PROXY
    }


REQUESTS_PROXIES = get_proxy()


# ============================================================
# AUTH
# ============================================================

def authorized_request():

    if not API_TOKEN:
        return True

    token = request.headers.get(
        "X-API-Token",
        ""
    )

    return token == API_TOKEN


# ============================================================
# CLEANUP
# ============================================================

def cleanup_old_files():

    now = time.time()

    try:

        for item in DOWNLOAD_DIR.iterdir():

            try:

                age = now - item.stat().st_mtime

                if age > FILE_MAX_AGE:

                    if item.is_file():
                        item.unlink()

                    elif item.is_dir():
                        import shutil
                        shutil.rmtree(item)

            except Exception:
                pass

    except Exception:
        pass


# ============================================================
# SAFE FILENAME
# ============================================================

def safe_filename(name):

    name = re.sub(
        r'[\\/*?:"<>|]',
        "",
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    ).strip()

    if not name:
        name = "download"

    return name[:100]


# ============================================================
# YOUTUBE URL VALIDATION
# ============================================================

def valid_youtube_url(url):

    try:

        parsed = urlparse(url)

        if parsed.scheme not in (
            "http",
            "https"
        ):
            return False

        host = parsed.netloc.lower().split(":")[0]

        return host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "www.youtu.be"
        }

    except Exception:

        return False


# ============================================================
# HEALTH
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "service": "YouTube Downloader API"
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok"
    }), 200


@app.route("/healthz")
def healthz():

    return jsonify({
        "status": "ok"
    }), 200


# ============================================================
# SEARCH
# ============================================================

@app.route("/search", methods=["GET"])
def search():

    if not authorized_request():

        return jsonify({
            "error": "Unauthorized"
        }), 401

    if not YOUTUBE_API_KEY:

        return jsonify({
            "error": "YouTube API key is not configured."
        }), 500

    query = request.args.get(
        "q",
        ""
    ).strip()

    if not query:

        return jsonify({
            "error": "Search query is required."
        }), 400

    if len(query) > 100:

        return jsonify({
            "error": "Search query is too long."
        }), 400

    try:

        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": 12,
            "safeSearch": "moderate",
            "key": YOUTUBE_API_KEY
        }

        response = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
            proxies=REQUESTS_PROXIES,
            timeout=15
        )

        data = response.json()

        if response.status_code != 200:

            return jsonify({
                "error": "YouTube API request failed.",
                "details": data.get(
                    "error",
                    {}
                ).get(
                    "message",
                    "Unknown error"
                )
            }), response.status_code

        results = []

        for item in data.get(
            "items",
            []
        ):

            video_id = item.get(
                "id",
                {}
            ).get(
                "videoId"
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
                thumbnails.get(
                    "high",
                    {}
                ).get("url")
                or
                thumbnails.get(
                    "medium",
                    {}
                ).get("url")
                or
                thumbnails.get(
                    "default",
                    {}
                ).get("url")
            )

            results.append({

                "id": video_id,

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

                "published": snippet.get(
                    "publishedAt",
                    ""
                ),

                "thumbnail": thumbnail,

                "url":
                    "https://www.youtube.com/watch?v="
                    + video_id
            })

        return jsonify({

            "success": True,

            "query": query,

            "count": len(results),

            "results": results

        })

    except requests.RequestException as e:

        return jsonify({
            "error": "Could not connect to YouTube.",
            "details": str(e)
        }), 502

    except Exception as e:

        return jsonify({
            "error": "Search failed.",
            "details": str(e)
        }), 500


# ============================================================
# DOWNLOAD
# ============================================================

@app.route("/download", methods=["GET"])
def download():

    if not authorized_request():

        return jsonify({
            "error": "Unauthorized"
        }), 401

    url = request.args.get(
        "url",
        ""
    ).strip()

    media_type = request.args.get(
        "type",
        "audio"
    ).lower()

    if not url:

        return jsonify({
            "error": "URL is required."
        }), 400

    if not valid_youtube_url(url):

        return jsonify({
            "error": "Only YouTube URLs are accepted."
        }), 400

    if media_type not in (
        "audio",
        "video"
    ):

        return jsonify({
            "error": "Type must be audio or video."
        }), 400

    cleanup_old_files()

    job_id = uuid.uuid4().hex

    output_template = str(
        DOWNLOAD_DIR /
        f"{job_id}.%(ext)s"
    )

    ydl_opts = {

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "restrictfilenames": True
    }

    # ========================================================
    # SOCKS5
    # ========================================================

    if SOCKS5_PROXY:

        ydl_opts["proxy"] = SOCKS5_PROXY


    # ========================================================
    # AUDIO
    # ========================================================

    if media_type == "audio":

        ydl_opts.update({

            "format": "bestaudio/best",

            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",

                    "preferredcodec": "mp3",

                    "preferredquality": "192"
                }
            ]
        })


    # ========================================================
    # VIDEO
    # ========================================================

    else:

        ydl_opts.update({

            "format": (
                "bestvideo[height<=1080]+"
                "bestaudio/"
                "best[height<=1080]/best"
            ),

            "merge_output_format": "mp4"
        })


    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            title = safe_filename(
                info.get(
                    "title",
                    "download"
                )
            )


        # ====================================================
        # FIND FILE
        # ====================================================

        files = [
            p
            for p in DOWNLOAD_DIR.glob(
                f"{job_id}.*"
            )
            if p.is_file()
        ]

        if not files:

            return jsonify({
                "error": "Downloaded file was not found."
            }), 500

        file_path = files[0]

        extension = file_path.suffix.lower()

        final_path = DOWNLOAD_DIR / (
            f"{title}-{job_id[:8]}"
            f"{extension}"
        )

        file_path.rename(
            final_path
        )


        return send_file(

            final_path,

            as_attachment=True,

            download_name=final_path.name
        )


    except yt_dlp.utils.DownloadError as e:

        for file in DOWNLOAD_DIR.glob(
            f"{job_id}.*"
        ):

            try:
                file.unlink()
            except Exception:
                pass

        return jsonify({

            "error":
                "The video could not be downloaded.",

            "details":
                str(e)

        }), 422


    except Exception as e:

        for file in DOWNLOAD_DIR.glob(
            f"{job_id}.*"
        ):

            try:
                file.unlink()
            except Exception:
                pass

        return jsonify({

            "error":
                "Download failed.",

            "details":
                str(e)

        }), 500


# ============================================================
# VERCEL ENTRYPOINT
# ============================================================

# Do NOT use app.run() on Vercel.
