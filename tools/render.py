#!/usr/bin/env python3
"""Reel Studio renderer.

Turns a self-contained reel HTML file into a 1080x1920 / 30 fps H.264 + AAC MP4.

Contract the HTML must satisfy (see docs/MASTER_PROMPT.md):
  window.REEL = {width, height, fps, duration, poster}
  window.__reelReady === true   when fonts/assets are ready  (or window.__reelError = "...")
  window.renderFrame(t)         draws the frame at time t (seconds), deterministic
  window.renderAudio()          -> Promise<base64 WAV> with the full soundtrack

Usage:
  python tools/render.py reels/stamp-wear.html
  python tools/render.py --auto            # used by GitHub Actions
  python tools/render.py reels/x.html --draft
  python tools/render.py reels/x.html --still 4.6
"""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def log(*a):
    print(*a, flush=True)


def all_reels() -> list[Path]:
    return sorted(p for p in Path("reels").glob("*.html") if not p.name.startswith("_"))


def auto_select() -> list[Path]:
    """Decide what to render from the GitHub Actions environment."""
    wanted = os.getenv("INPUT_REEL", "").strip()
    if wanted:
        p = Path(wanted)
        if not p.exists():
            p = Path("reels") / wanted
        return [p]
    if os.getenv("EVENT_NAME") == "push":
        before, sha = os.getenv("BEFORE", ""), os.getenv("SHA", "HEAD")
        if before and set(before) != {"0"}:
            try:
                names = subprocess.check_output(
                    ["git", "diff", "--name-only", before, sha, "--", "reels/"], text=True
                ).split()
                files = [Path(n) for n in names if n.endswith(".html")]
                files = [p for p in files if p.exists() and not p.name.startswith("_")]
                return sorted(files)
            except subprocess.CalledProcessError:
                pass
    return all_reels()


def render_reel(html: Path, out_dir: Path, draft: bool = False, still: float | None = None) -> None:
    if not html.exists():
        raise FileNotFoundError(html)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = html.stem
    dsf = 0.5 if draft else 1
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--font-render-hinting=none",
                "--force-color-profile=srgb",
                "--autoplay-policy=no-user-gesture-required",
            ]
        )
        ctx = browser.new_context(viewport={"width": 1080, "height": 1920}, device_scale_factor=dsf)
        page = ctx.new_page()
        page.set_default_timeout(120_000)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: log("  [page]", m.text) if m.type in ("error", "warning") else None)

        log(f"→ {html.name}: loading")
        page.goto(html.resolve().as_uri() + "?render=1", wait_until="load")
        page.wait_for_function("window.__reelReady === true || !!window.__reelError", timeout=180_000)
        err = page.evaluate("window.__reelError || null")
        if err or errors:
            raise RuntimeError("page error: " + (err or "; ".join(errors)))

        cfg = page.evaluate("window.REEL")
        for k in ("width", "height", "fps", "duration"):
            if k not in (cfg or {}):
                raise RuntimeError(f"window.REEL is missing '{k}'")
        width, height, fps, duration = cfg["width"], cfg["height"], cfg["fps"], cfg["duration"]
        if (width, height) != (1080, 1920):
            page.set_viewport_size({"width": width, "height": height})

        if still is not None:
            page.evaluate("t => window.renderFrame(t)", float(still))
            target = out_dir / f"{stem}_still.png"
            target.write_bytes(page.screenshot(type="png"))
            log(f"✓ still saved: {target}")
            browser.close()
            return

        tmp = Path(tempfile.mkdtemp(prefix="reel_"))
        wav = tmp / "audio.wav"
        has_audio = page.evaluate("typeof window.renderAudio === 'function'")
        if has_audio:
            log("  rendering soundtrack")
            b64 = page.evaluate("async () => await window.renderAudio()")
            wav.write_bytes(base64.b64decode(b64))

        step = 2 if draft else 1
        out_fps = fps / step
        out_w, out_h = (width // 2, height // 2) if draft else (width, height)
        mp4 = out_dir / f"{stem}.mp4"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "image2pipe", "-framerate", str(out_fps), "-c:v", "png", "-i", "pipe:0"]
        if has_audio:
            cmd += ["-i", str(wav)]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += [
            "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
            "-c:v", "libx264", "-profile:v", "high",
            "-preset", "veryfast" if draft else "slow",
            "-crf", "25" if draft else "16",
            "-r", str(out_fps),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-t", f"{duration:.3f}", "-movflags", "+faststart", str(mp4),
        ]
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        total = round(duration * fps)
        started = time.time()
        log(f"  capturing {total // step} frames at {out_fps:g} fps ({out_w}x{out_h})")
        try:
            for n, i in enumerate(range(0, total, step)):
                page.evaluate("t => window.renderFrame(t)", i / fps)
                ff.stdin.write(page.screenshot(type="png"))
                if n % 60 == 0 and n:
                    done = n / (total // step)
                    eta = (time.time() - started) / done * (1 - done)
                    log(f"  {int(done * 100)}%  (~{int(eta)}s left)")
            ff.stdin.close()
        except BrokenPipeError:
            pass
        rc = ff.wait()
        if rc != 0:
            raise RuntimeError("ffmpeg failed: " + ff.stderr.read().decode(errors="replace"))

        poster = cfg.get("poster", duration / 2)
        page.evaluate("t => window.renderFrame(t)", float(poster))
        (out_dir / f"{stem}.jpg").write_bytes(page.screenshot(type="jpeg", quality=92))
        browser.close()
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"✓ {mp4}  ({mp4.stat().st_size / 1e6:.1f} MB, {time.time() - started:.0f}s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--auto", action="store_true", help="pick reels from the GitHub Actions environment")
    ap.add_argument("--draft", action="store_true", help="quick half-size 15 fps preview render")
    ap.add_argument("--still", type=float, help="save a single PNG at this second and stop")
    ap.add_argument("--out", type=Path, default=Path("output"))
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        log("ffmpeg not found on PATH")
        return 2
    files = auto_select() if args.auto else args.files
    if not files:
        log("Nothing to render.")
        return 0
    failed = []
    for f in files:
        try:
            render_reel(f, args.out, draft=args.draft, still=args.still)
        except Exception as e:  # keep going so one bad file doesn't block the rest
            log(f"✗ {f}: {e}")
            failed.append(f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
