import glob
import json
import os
import shutil
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

import gradio as gr
import yt_dlp

OUTPUT_ROOT = "output"
MAX_AGE_SECONDS = 3600  # delete old downloads after 1 hour
MAX_COOKIE_JSON_BYTES = 2 * 1024 * 1024
ALLOWED_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
QUALITIES = ["128kbps", "192kbps", "256kbps", "320kbps"]

os.makedirs(OUTPUT_ROOT, exist_ok=True)

# Optional proxy can be configured through an environment secret.
PROXY = os.environ.get("YT_PROXY")


def check_youtube():
    """Log whether YouTube is reachable from this server."""
    try:
        urllib.request.urlopen("https://www.youtube.com", timeout=10)
        print("[check] YouTube reachable")
    except Exception as e:
        print(f"[check] YouTube NOT reachable: {e!r}")


def cleanup_old_downloads():
    now = time.time()
    for path in glob.glob(os.path.join(OUTPUT_ROOT, "*")):
        if os.path.isdir(path) and now - os.path.getmtime(path) > MAX_AGE_SECONDS:
            shutil.rmtree(path, ignore_errors=True)


def cookies_json_to_netscape(cookie_json_file=None, pasted_cookie_json=None):
    """Convert pasted or uploaded browser-exported JSON for yt-dlp's temporary use."""
    pasted_cookie_json = (pasted_cookie_json or "").strip()
    if cookie_json_file and pasted_cookie_json:
        raise gr.Error("Use either pasted JSON or the file upload, not both.")

    if pasted_cookie_json:
        if len(pasted_cookie_json.encode("utf-8")) > MAX_COOKIE_JSON_BYTES:
            raise gr.Error("The cookie JSON must be 2 MB or smaller.")
        raw_json = pasted_cookie_json
    elif cookie_json_file:
        if os.path.splitext(cookie_json_file)[1].lower() != ".json":
            raise gr.Error("Upload cookies as a .json file.")
        try:
            if os.path.getsize(cookie_json_file) > MAX_COOKIE_JSON_BYTES:
                raise gr.Error("The cookie JSON file must be 2 MB or smaller.")
            with open(cookie_json_file, "r", encoding="utf-8") as f:
                raw_json = f.read()
        except gr.Error:
            raise
        except (OSError, UnicodeDecodeError):
            raise gr.Error("Couldn't read the uploaded cookie JSON.")
    else:
        return None

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        raise gr.Error("Couldn't parse the cookie input as valid JSON.")

    if isinstance(data, list):
        cookies = data
    elif isinstance(data, dict) and isinstance(data.get("cookies"), list):
        cookies = data["cookies"]
    else:
        raise gr.Error("Cookie JSON must be a list or an object with a cookies list.")

    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise gr.Error("Every cookie entry must be a JSON object.")

        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise gr.Error("Each cookie needs a name and string value.")
        if not isinstance(domain, str) or not domain.strip():
            raise gr.Error("Each cookie needs a domain.")

        normalized_domain = domain.strip().lower().lstrip(".").rstrip(".")
        if not (
            normalized_domain == "youtube.com"
            or normalized_domain.endswith(".youtube.com")
            or normalized_domain == "google.com"
            or normalized_domain.endswith(".google.com")
        ):
            continue

        path = cookie.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/"):
            raise gr.Error("Cookie paths must start with '/'.")
        if any(char in value or char in name or char in path for char in "\t\r\n"):
            raise gr.Error("Cookie fields cannot contain tabs or line breaks.")

        host_only = cookie.get("hostOnly")
        include_subdomains = (
            not host_only if isinstance(host_only, bool) else domain.strip().startswith(".")
        )
        netscape_domain = f".{normalized_domain}" if include_subdomains else normalized_domain
        if cookie.get("httpOnly"):
            netscape_domain = f"#HttpOnly_{netscape_domain}"

        secure = "TRUE" if cookie.get("secure", False) else "FALSE"
        expires = cookie.get("expirationDate", cookie.get("expires", 0))
        if cookie.get("session") or expires is None or expires == "":
            expires = 0
        try:
            expires = max(0, int(float(expires)))
        except (TypeError, ValueError, OverflowError):
            raise gr.Error("Cookie expiration dates must be numeric.")

        lines.append(
            "\t".join(
                [
                    netscape_domain,
                    "TRUE" if include_subdomains else "FALSE",
                    path,
                    secure,
                    str(expires),
                    name,
                    value,
                ]
            )
        )

    if len(lines) == 1:
        raise gr.Error("No YouTube or Google cookies were found in that JSON.")
    return "\n".join(lines) + "\n"


def download_audio(
    youtubelink,
    quality,
    cookie_mode="paste",
    pasted_cookie_json=None,
    cookie_json_file=None,
):
    if not youtubelink or not youtubelink.strip():
        raise gr.Error("Please paste a YouTube link.")

    parsed_url = urlparse(youtubelink.strip())
    hostname = (parsed_url.hostname or "").lower()
    if (
        parsed_url.scheme != "https"
        or hostname not in ALLOWED_YOUTUBE_HOSTS
        or parsed_url.username
        or parsed_url.password
    ):
        raise gr.Error("Please enter a valid HTTPS link from YouTube.")

    if quality not in QUALITIES:
        raise gr.Error("Please select a supported audio quality.")

    if cookie_mode == "paste":
        cookie_text = cookies_json_to_netscape(pasted_cookie_json=pasted_cookie_json)
    elif cookie_mode == "upload":
        cookie_text = cookies_json_to_netscape(cookie_json_file=cookie_json_file)
    else:
        raise gr.Error("Choose a cookie input tab.")
    cleanup_old_downloads()
    bitrate = quality.replace("kbps", "")
    job_dir = tempfile.mkdtemp(dir=OUTPUT_ROOT)

    ydl_opts = {
        "format": "bestaudio/best",
        "js_runtimes": {"node": {}},
        "outtmpl": f"{job_dir}/%(title).100s.%(ext)s",
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": False,
        "retries": 3,
        "socket_timeout": 20,
        "cachedir": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,
            },
            {"key": "FFmpegMetadata"},
        ],
    }
    cookie_file = None
    if cookie_text:
        cookie_file = os.path.join(job_dir, "cookies.txt")
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookie_text)
        ydl_opts["cookiefile"] = cookie_file
    if PROXY:
        ydl_opts["proxy"] = PROXY

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(youtubelink.strip(), download=True)
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        msg = str(e)
        if "SSL" in msg or "UNEXPECTED_EOF" in msg:
            raise gr.Error(
                "This server can't connect to YouTube (network/TLS blocked). "
                "Try again later or use a different host/proxy."
            )
        if "Sign in to confirm" in msg or "bot" in msg.lower():
            raise gr.Error("YouTube is asking for a login (bot check). Cookies are required.")
        raise gr.Error(f"Download failed: {msg[:300]}")
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        if cookie_file and os.path.exists(cookie_file):
            os.remove(cookie_file)

    files = glob.glob(os.path.join(job_dir, "*.mp3"))
    if not files:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise gr.Error("Conversion finished but no MP3 was produced (is ffmpeg installed?).")
    return files[0], "", None


with gr.Blocks() as demo:
    gr.Markdown("## YouTube to MP3 Downloader")
    gr.Markdown("Convert a YouTube video to an MP3 for content you have permission to download.")

    url_input = gr.Textbox(label="YouTube URL", placeholder="Paste YouTube link here...")
    quality_input = gr.Radio(
        QUALITIES,
        value="256kbps",
        label="Select Quality",
    )
    cookie_mode = gr.State(value="paste")
    with gr.Tabs():
        with gr.Tab("Paste JSON") as paste_cookie_tab:
            cookie_text_input = gr.Textbox(
                label="YouTube cookies JSON",
                placeholder="Paste browser-exported JSON here...",
                lines=8,
                max_lines=16,
            )
        with gr.Tab("Upload JSON") as upload_cookie_tab:
            cookie_file_input = gr.File(
                label="YouTube cookies JSON file",
                file_types=[".json"],
                type="filepath",
            )
    paste_cookie_tab.select(
        lambda: "paste",
        outputs=cookie_mode,
        queue=False,
    )
    upload_cookie_tab.select(
        lambda: "upload",
        outputs=cookie_mode,
        queue=False,
    )
    gr.Markdown(
        "Use either tab. Paste a JSON array or an object with a `cookies` array. "
        "Cookies grant account access; the inputs are cleared after a successful download."
    )
    download_btn = gr.Button("Download & Convert")
    audio_output = gr.Audio(label="Downloaded MP3", type="filepath")

    download_btn.click(
        fn=download_audio,
        inputs=[url_input, quality_input, cookie_mode, cookie_text_input, cookie_file_input],
        outputs=[audio_output, cookie_text_input, cookie_file_input],
    )

if __name__ == "__main__":
    check_youtube()
    demo.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", "5000")),
        ssr_mode=False,
    )