import os
import re
import uuid
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv


# ==================================================
# Configuration
# ==================================================

load_dotenv()

app = Flask(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
API_TOKEN = os.getenv("API_TOKEN", "").strip()
SOCKS5_PROXY = os.getenv("SOCKS5_PROXY", "").strip()

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Delete files older than 5 minutes
FILE_MAX_AGE = 300


# ==================================================
# Proxy
# ==================================================

def get_requests_proxies():
    """
    Configure SOCKS5 proxy for requests.

    Example:
    socks5h://127.0.0.1:1080
    """

    if not SOCKS5_PROXY:
        return None

    return {
        "http": SOCKS5_PROXY,
        "https": SOCKS5_PROXY
    }


REQUESTS_PROXIES = get_requests_proxies()


# ==================================================
# Authorization
# ==================================================

def authorized_request():

    if not API_TOKEN:
        return True

    supplied = request.headers.get(
        "X-API-Token",
        ""
    )

    return supplied == API_TOKEN


# ==================================================
# YouTube URL Validation
# ==================================================

def valid_youtube_url(url):

    try:

        parsed = urlparse(url)

        if parsed.scheme not in (
            "http",
            "https"
        ):
            return False

        host = parsed.netloc.lower().split(":")[0]

        allowed_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }

        return host in allowed_hosts

    except Exception:

        return False


# ==================================================
# Safe Filename
# ==================================================

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

    return name[:120]


# ==================================================
# Cleanup
# ==================================================

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
                        shutil.rmtree(item)

            except (
                FileNotFoundError,
                PermissionError
            ):

                pass

            except Exception:

                pass

    except Exception:

        pass


# ==================================================
# Health
# ==================================================

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


# ==================================================
# YouTube Search
# ==================================================

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

                "thumbnail": (
                    snippet.get(
                        "thumbnails",
                        {}
                    ).get(
                        "high",
                        {}
                    ).get("url")

                    or

                    snippet.get(
                        "thumbnails",
                        {}
                    ).get(
                        "medium",
                        {}
                    ).get("url")
                ),

                "url": (
                    "https://www.youtube.com/watch?v="
                    + video_id
                )
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


# ==================================================
# Download
# ==================================================

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

    # Clean old files whenever the function runs
    cleanup_old_files()

    job_id = uuid.uuid4().hex

    output_template = str(
        DOWNLOAD_DIR /
        f"{job_id}.%(ext)s"
    )

    try:

        # ==================================================
        # yt-dlp Options
        # ==================================================

        ydl_opts = {

            "outtmpl": output_template,

            "noplaylist": True,

            "quiet": True,

            "no_warnings": True,

            "restrictfilenames": True,

            "js_runtimes": {
                "deno": {}
            }
        }

        # Add SOCKS5 proxy only when configured
        if SOCKS5_PROXY:

            ydl_opts["proxy"] = SOCKS5_PROXY


        # ==================================================
        # Audio
        # ==================================================

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


        # ==================================================
        # Video
        # ==================================================

        else:

            ydl_opts.update({

                "format": (
                    "bestvideo[height<=1080]+"
                    "bestaudio/"
                    "best[height<=1080]/best"
                ),

                "merge_output_format": "mp4"

            })


        # ==================================================
        # Download
        # ==================================================

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


        # ==================================================
        # Find File
        # ==================================================

        possible_files = list(
            DOWNLOAD_DIR.glob(
                f"{job_id}.*"
            )
        )

        possible_files = [
            p
            for p in possible_files
            if p.is_file()
        ]

        if not possible_files:

            return jsonify({
                "error": (
                    "Download completed but output "
                    "file was not found."
                )
            }), 500

        file_path = possible_files[0]

        extension = file_path.suffix.lower()

        final_path = DOWNLOAD_DIR / (
            f"{title}-{job_id[:8]}{extension}"
        )

        file_path.rename(
            final_path
        )


        # ==================================================
        # Return File
        # ==================================================

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

            "error": (
                "The video could not be downloaded."
            ),

            "details": str(e)

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

            "error": "Download failed.",

            "details": str(e)

        }), 500
