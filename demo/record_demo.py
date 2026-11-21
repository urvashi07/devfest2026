"""
Records a backup demo video of PlumeScout: the chat in the ADK web UI, then the
guided tour of the generated 2D/3D hazard map.

Needs both servers running (adk web on :8000, the maps server on :8010) and:
    pip install playwright imageio-ffmpeg
    python -m playwright install chromium

Usage (from the repo root):
    .venv\\Scripts\\python.exe demo\\record_demo.py
    .venv\\Scripts\\python.exe demo\\record_demo.py --prompt "Chlorine leak at 51.9515, 7.6410"

A Chrome window opens for about 2 minutes while recording; don't click in it.
Output: demo/plumescout_demo.mp4
"""
import argparse
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ADK_URL = "http://127.0.0.1:8000/dev-ui/?app=plume_scout"  # not "localhost": Chrome may try IPv6 first
MAPS_URL = "http://127.0.0.1:8010"
DEFAULT_PROMPT = "Large warehouse fire with heavy smoke at 51.9515, 7.6410"
SIZE = {"width": 1280, "height": 720}
TOUR_SECONDS = 82  # 9 steps (~75 s) plus camera flights
OUT = Path(__file__).resolve().parent / "plumescout_demo.mp4"


def record(prompt: str, headed: bool) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="plumescout_rec_"))
    with sync_playwright() as p:
        # A visible window can use the GPU; headless needs software WebGL for the 3D map
        soft_gl = [] if headed else ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
        browser = p.chromium.launch(headless=not headed, args=soft_gl)
        ctx = browser.new_context(viewport=SIZE, record_video_dir=str(tmp), record_video_size=SIZE)
        page = ctx.new_page()

        # 1) Chat: ask the agent
        page.goto(ADK_URL)
        box = page.get_by_placeholder("Type a message...")
        box.wait_for(timeout=30000)
        time.sleep(1.5)
        box.click()
        box.type(prompt, delay=55)
        time.sleep(0.6)
        box.press("Enter")

        # Wait until the map artifact exists, then give the briefing time to stream in
        page.get_by_text(re.compile(r"Artifact: plume_map_\d{8}_\d{6}\.html")).first.wait_for(timeout=180000)
        time.sleep(8)
        map_file = re.search(r"plume_map_\d{8}_\d{6}\.html", page.content()).group(0)
        print("map:", map_file)

        # Scroll through the briefing slowly: jump to the top of the chat panel, then glide down
        page.evaluate("""() => {
            const panels = [...document.querySelectorAll('*')].filter(e =>
                e.scrollHeight > e.clientHeight + 50 && /auto|scroll/.test(getComputedStyle(e).overflowY)
                && e.innerText.includes('MP-1'));
            panels.sort((a, b) => a.scrollHeight - b.scrollHeight);  // innermost panel holding the briefing
            window.__chat = panels[0];
            window.__chat.scrollTop = 0;
        }""")
        time.sleep(3)
        for _ in range(60):
            done = page.evaluate("""() => { const c = window.__chat; c.scrollTop += 28;
                                            return c.scrollTop + c.clientHeight >= c.scrollHeight - 2; }""")
            time.sleep(0.25)
            if done:
                break
        time.sleep(2.5)

        # 2) Map: guided tour (starts itself with #tour)
        page.goto(f"{MAPS_URL}/{map_file}#tour")
        time.sleep(TOUR_SECONDS)

        video = page.video.path()
        ctx.close()
        browser.close()
    return Path(video)


def to_mp4(webm: Path, out: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(webm),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(out)],
        check=True,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--headless", action="store_true",
                    help="hide the browser window (software 3D rendering: buildings may load too slowly)")
    args = ap.parse_args()

    webm = record(args.prompt, headed=not args.headless)
    to_mp4(webm, OUT)
    shutil.rmtree(webm.parent, ignore_errors=True)
    print("video:", OUT)
