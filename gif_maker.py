#!/usr/bin/env python3
"""
GIF Maker - a cross-platform (Windows / macOS / Linux) video-to-GIF converter.
Also exports animated WebP, MP4 and WebM, which are much smaller than GIF.

Requirements:
    Python 3.8+ (with Tkinter, included in the python.org installers)
    imageio-ffmpeg -> bundles an FFmpeg binary (no separate install needed)
    pillow         -> used to play the animated preview
    tkinterdnd2    -> drag and drop (optional: the app works without it)
    The app offers to install them on first run. Manual install, using the Python that runs the app:
        python -m pip install imageio-ffmpeg pillow tkinterdnd2

Run:
    python gif_maker.py
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

Image = ImageSequence = ImageTk = None


def load_pillow():
    """Import Pillow into module globals. Returns True if available.

    Done in a function (not at import time) so it can be retried right after
    the in-app installer adds it, without restarting the program.
    """
    global Image, ImageSequence, ImageTk
    try:
        from PIL import Image, ImageSequence, ImageTk  # noqa: F811
        return True
    except ImportError:
        return False


load_pillow()

VIDEO_TYPES = [
    ("Video files", "*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.wmv *.flv *.mpg *.mpeg *.3gp"),
    ("All files", "*.*"),
]
DITHER_MODES = ["sierra2_4a", "floyd_steinberg", "bayer", "none"]
RESOLUTIONS = ["Original", "1080p", "720p", "480p", "360p", "240p", "144p", "Custom width"]
LOOP_CHOICES = ["Forever", "Play once", "2", "3", "5"]

# Output formats. 'kind' decides how it's encoded and previewed:
#   image = animated image (GIF / WebP): loops by itself, decoded by Pillow
#   video = real video file (MP4 / WebM): far smaller, can carry sound
FORMATS = {
    "GIF": dict(ext="gif", kind="image", encoder=None,
                about="Plays and loops by itself everywhere: texts, Discord, email. Largest files."),
    "WebP": dict(ext="webp", kind="image", encoder="libwebp_anim",
                 about="Animated image: plays and loops by itself like a GIF, usually 3–5× "
                       "smaller with full color. Good for the web and Discord; patchy in "
                       "email and texts."),
    "MP4": dict(ext="mp4", kind="video", encoder="libx264",
                about="Video. Smallest files and opens almost anywhere, but most chat apps "
                      "(iMessage, RCS, Discord) show it as a video to tap Play on, not an "
                      "auto-looping GIF. Can keep sound."),
    "WebM": dict(ext="webm", kind="video", encoder="libvpx-vp9",
                 about="Video like MP4, often a bit smaller, also tap-to-play in chats. "
                       "Good for Discord and the web; iPhones often can't play it."),
}
AUDIO = {"mp4": ("aac", 128_000), "webm": ("libopus", 96_000)}  # codec, bits per second

MIN_WIDTH = 120  # the size-targeting loop won't shrink below this
MIN_FPS = 5
MIN_WEBP_QUALITY = 40
MAX_PASSES = 8
PREVIEW_MEMORY_BUDGET = 400 * 1024 * 1024  # bytes of decoded frames to pre-load; above this, stream
PALETTE_MAX_WIDTH = 640  # GIF palette is built from a downscaled copy: same colors, much faster


# --------------------------------------------------------------------------- #
#  FFmpeg helpers
# --------------------------------------------------------------------------- #
# Hide the console window that would otherwise flash up on Windows.
_POPEN_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def ffmpeg_check(path):
    """Return (usable, version_string) for an FFmpeg binary.

    'Usable' means it has the palettegen/paletteuse filters GIF output relies
    on (FFmpeg 2.6+, 2015). Old copies bundled with other software often don't.
    """
    try:
        ver = subprocess.run([path, "-version"], capture_output=True, text=True,
                             errors="replace", timeout=15, **_POPEN_FLAGS).stdout
        filters = subprocess.run([path, "-filters"], capture_output=True, text=True,
                                 errors="replace", timeout=15, **_POPEN_FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        return False, "couldn't run"
    m = re.search(r"version\s+(\S+)", ver)
    return ("palettegen" in filters and "paletteuse" in filters), (m[1] if m else "unknown")


def find_ffmpeg():
    """Return (path, description) of a usable FFmpeg, or (None, reason).

    Prefers the copy bundled by imageio-ffmpeg (known-good, recent), and only
    accepts one from PATH if it's new enough.
    """
    try:
        import importlib
        importlib.invalidate_caches()  # see packages installed since startup
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        ok, ver = ffmpeg_check(path)
        if ok:
            return path, f"FFmpeg {ver} (bundled)"
    except Exception:
        pass
    path = shutil.which("ffmpeg")
    if path:
        ok, ver = ffmpeg_check(path)
        if ok:
            return path, f"FFmpeg {ver} ({path})"
        return None, f"The FFmpeg found at {path} (version {ver}) is too old."
    return None, "FFmpeg isn't installed."


def available_encoders(ffmpeg):
    """Names of the encoders this FFmpeg build includes (e.g. 'libx264').

    Builds differ, so we check rather than assume; formats whose encoder is
    missing are shown as unavailable instead of failing mid-conversion.
    """
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                             text=True, errors="replace", timeout=15, **_POPEN_FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {m[1] for m in re.finditer(r"^\s*[VAS][.A-Z]{5}\s+(\S+)", out, re.M)}


def install_requirements():
    """pip-install the dependencies into *this* Python interpreter.

    Using sys.executable guarantees they land in the same Python that's running
    the app — the usual cause of 'I installed it but it's not found' is having
    several Pythons, where `pip` and double-clicking the script use different ones.
    """
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):  # pythonw has no console; use python.exe for pip
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    r = subprocess.run([exe, "-m", "pip", "install", "--upgrade", "imageio-ffmpeg", "pillow", "tkinterdnd2"],
                       capture_output=True, text=True, errors="replace", **_POPEN_FLAGS)
    add_user_site()
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-3:]


def add_user_site():
    """Make packages pip just put in the per-user folder importable right away.

    When Python's own folder isn't writable (e.g. installed under Program
    Files), pip installs to the user folder instead (%APPDATA%\\Python\\...
    on Windows, ~/Library/Python/... on macOS). Python only adds that folder
    to sys.path at startup if it already existed, so on a first install it
    isn't searched until the app is restarted.
    """
    import site
    path = site.getusersitepackages() if site.ENABLE_USER_SITE else None  # off in venvs
    if path and os.path.isdir(path) and path not in sys.path:
        site.addsitedir(path)


def probe_video(ffmpeg, path):
    """Return (duration, width, height, fps, has_audio, error) by parsing `ffmpeg -i`.

    This only reads the container header, so it's instant even for huge files
    (unlike decoding every frame to count them).
    """
    out = subprocess.run([ffmpeg, "-i", path],
                         capture_output=True, text=True, errors="replace",
                         **_POPEN_FLAGS).stderr
    dur = w = h = fps = None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    if m:
        w, h = int(m[1]), int(m[2])
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", out)
    if m:
        fps = float(m[1])
    has_audio = bool(re.search(r"Stream #.*Audio:", out))
    return dur, w, h, fps, has_audio, (None if dur or w else explain_probe_error(out))


def explain_probe_error(ffmpeg_output):
    """Turn FFmpeg's error output into a short, human-readable reason."""
    text = ffmpeg_output.lower()
    if "moov atom not found" in text or "invalid data found" in text:
        return ("The file looks incomplete or damaged — common if the recording was "
                "interrupted or is still being written (e.g. OBS still recording). "
                "Try another file, or remux/repair it.")
    if "permission denied" in text:
        return "Permission denied — the file may be open in another program."
    if "no such file" in text:
        return "File not found."
    lines = [l.strip() for l in ffmpeg_output.strip().splitlines() if l.strip()]
    return lines[-1] if lines else "FFmpeg returned no information."


def resolve_width(preset, custom, src_w, src_h):
    """Turn a resolution choice into an output width (None = keep original).

    Presets like '720p' describe the *height*, so we convert to a width using
    the source's aspect ratio. We never upscale: a 480p preset on a 360p video
    just keeps 360p, since upscaling only adds file size, not detail.
    """
    if preset == "Original":
        return None
    if preset == "Custom width":
        custom = custom.strip()
        if not custom:
            return None
        w = int(custom)
        if w < 16:
            raise ValueError("Width must be at least 16 px.")
        return w // 2 * 2
    target_h = int(preset.rstrip("p"))
    if src_w and src_h:
        if target_h >= src_h:
            return None
        return round(src_w * target_h / src_h) // 2 * 2
    return round(target_h * 16 / 9) // 2 * 2  # unknown source: assume 16:9


# ---- command builders ------------------------------------------------------ #
def _input_args(src, start, duration):
    # -ss before -i = fast keyframe seek; -t limits how much source is read.
    return ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", src]


def _video_filters(fps, width, speed, reverse=False, even=False):
    filters = []
    if speed != 1.0:
        filters.append(f"setpts=PTS/{speed}")
    filters.append(f"fps={fps}")  # drop frames early so later filters do less work
    if width:
        # -2 keeps the aspect ratio and forces an even height
        filters.append(f"scale={width}:-2:flags=lanczos")
    elif even:
        # Video codecs (4:2:0 color) need even dimensions; trim 1 px if the source is odd.
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    if reverse:
        # 'reverse' must buffer the whole clip, so do it last, after fps/scale
        # have made the frames as few and as small as possible.
        filters.append("reverse")
    return filters


def _audio_filters(speed, reverse):
    """atempo changes speed without changing pitch, but only accepts 0.5–2.0
    per instance (older FFmpeg), so larger changes are chained: 4× = 2× then 2×."""
    filters, s = [], speed
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        filters.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-6:
        filters.append(f"atempo={s:.6f}")
    if reverse:
        filters.append("areverse")
    return filters


def build_palette_command(ffmpeg, o, palette, fps, width):
    """GIF pass 1: scan the clip and write its best palette to a PNG.

    A custom palette per GIF is the main reason FFmpeg GIFs look far better
    than a generic 256-color one. palettegen only keeps color statistics,
    not frames, so memory stays flat however long the clip is.
    """
    pw = min(width, PALETTE_MAX_WIDTH) if width else PALETTE_MAX_WIDTH
    vf = ",".join(_video_filters(fps, pw, o["speed"]) +
                  [f"palettegen=max_colors={o['colors']}:stats_mode=diff"])
    return [ffmpeg, "-y", *_input_args(o["src"], o["start"], o["duration"]),
            "-vf", vf, "-frames:v", "1", "-update", "1", palette]


def build_gif_command(ffmpeg, o, palette, fps, width):
    """GIF pass 2: convert the clip using the palette from pass 1.

    Why two passes instead of one (split -> palettegen -> paletteuse)?
    In a single pass, palettegen can't output anything until it has seen the
    LAST frame, so FFmpeg has to hold every frame in memory meanwhile — about
    7 GB for 30 s of 1080p. Two passes decode the video twice but use almost
    no memory, and the palette can be reused for the file-size retries.
    """
    filters = _video_filters(fps, width, o["speed"], o["reverse"])
    dither = o["dither"]
    dither_opt = f"dither={dither}" + (":bayer_scale=3" if dither == "bayer" else "")
    graph = (f"[0:v]{','.join(filters)}[v];"
             f"[v][1:v]paletteuse={dither_opt}:diff_mode=rectangle")
    # GIF loop field: 0 = forever, -1 = play once, N = repeat N more times
    loop = {"forever": 0, "once": -1}.get(o["loop"], o["loop"] - 1 if isinstance(o["loop"], int) else 0)
    return [ffmpeg, "-y", *_input_args(o["src"], o["start"], o["duration"]),
            "-i", palette, "-filter_complex", graph, "-loop", str(loop), "-an", o["dst"]]


def build_webp_command(ffmpeg, o, fps, width, quality, encoder):
    vf = ",".join(_video_filters(fps, width, o["speed"], o["reverse"]))
    # WebP loop count = total plays, 0 = forever
    loop = {"forever": 0, "once": 1}.get(o["loop"], o["loop"] if isinstance(o["loop"], int) else 0)
    return [ffmpeg, "-y", *_input_args(o["src"], o["start"], o["duration"]),
            "-vf", vf, "-c:v", encoder, "-lossless", "0", "-quality", str(quality),
            "-compression_level", "4", "-loop", str(loop), "-an", o["dst"]]


def quality_to_crf(fmt, quality):
    """Map the 1–100 quality slider to each codec's CRF scale (lower = better).

    CRF ('constant rate factor') keeps quality steady and lets file size vary,
    which is what you want when there's no size limit.
    """
    if fmt == "mp4":   # x264: 18 ≈ visually lossless, 28 = clearly soft
        return round(40 - quality * 0.22)
    return round(50 - quality * 0.33)  # VP9: 17 (q100) .. 50


def build_video_command(ffmpeg, o, fps, width, quality, bitrate=None):
    """MP4 (H.264) or WebM (VP9). With `bitrate` set, encode to that average
    bitrate instead of a quality level — used to hit a file-size limit."""
    ext = o["ext"]
    vf = ",".join(_video_filters(fps, width, o["speed"], o["reverse"], even=True))
    cmd = [ffmpeg, "-y", *_input_args(o["src"], o["start"], o["duration"]),
           "-map", "0:v:0", "-vf", vf, "-pix_fmt", "yuv420p"]
    if ext == "mp4":
        # yuv420p + faststart = plays on phones and starts before fully downloaded
        cmd += ["-c:v", "libx264", "-preset", "medium", "-movflags", "+faststart"]
        cmd += (["-b:v", str(bitrate), "-maxrate", str(int(bitrate * 1.5)),
                 "-bufsize", str(bitrate * 2)] if bitrate else
                ["-crf", str(quality_to_crf("mp4", quality))])
    else:
        cmd += ["-c:v", "libvpx-vp9", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"]
        cmd += (["-b:v", str(bitrate)] if bitrate else
                ["-crf", str(quality_to_crf("webm", quality)), "-b:v", "0"])
    if o["audio"]:
        codec, abr = AUDIO[ext]
        af = _audio_filters(o["speed"], o["reverse"])
        cmd += ["-map", "0:a:0", "-c:a", codec, "-b:a", str(abr)] + (["-af", ",".join(af)] if af else [])
    else:
        cmd += ["-an"]
    return cmd + [o["dst"]]


# ---- preview frame sources ------------------------------------------------- #
def fit_size(w, h, max_w, max_h):
    scale = min(1.0, max_w / w, max_h / h)
    return max(1, int(w * scale)), max(1, int(h * scale))


def frame_for_display(frame, size):
    """Composite an animated-image frame to RGB and fit it to the preview size.

    GIF/WebP frames often store only the changed rectangle; Pillow composites
    it onto the previous frames when converting, giving the full picture.
    """
    f = frame.convert("RGB")
    return f if f.size == size else f.resize(size, Image.BILINEAR)


class ListSource:
    """All frames already decoded in memory: smoothest playback."""

    def __init__(self, frames, delays):
        self.frames, self.delays, self.i = frames, delays, 0

    def next(self):
        f, d = self.frames[self.i], self.delays[self.i]
        self.i = (self.i + 1) % len(self.frames)
        return f, d

    def close(self):
        pass


class ImageStream:
    """Decodes a big GIF/WebP one frame at a time from disk."""

    def __init__(self, path, size, n):
        self.im, self.size, self.n, self.i = Image.open(path), size, n, 0

    def next(self):
        self.im.seek(self.i)  # forward seeks are cheap; seek(0) restarts the loop
        self.i = (self.i + 1) % self.n
        return frame_for_display(self.im, self.size), self.im.info.get("duration") or 100

    def close(self):
        self.im.close()  # must close before deleting the file on Windows


class VideoStream:
    """Decodes an MP4/WebM by asking FFmpeg for raw RGB frames through a pipe.

    Pillow can't read video, but FFmpeg can output every frame as plain
    width*height*3 bytes, which Pillow turns straight into an image.
    """

    def __init__(self, ffmpeg, path, size, fps):
        self.ffmpeg, self.path, self.size = ffmpeg, path, size
        self.delay = max(1, round(1000 / fps))
        self.frame_bytes = size[0] * size[1] * 3
        self.proc = None

    def _start(self):
        self.close()
        self.proc = subprocess.Popen(
            [self.ffmpeg, "-v", "error", "-i", self.path, "-vf",
             f"scale={self.size[0]}:{self.size[1]}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_POPEN_FLAGS)

    def read_frame(self):
        data = self.proc.stdout.read(self.frame_bytes)
        if len(data) < self.frame_bytes:
            return None
        return Image.frombytes("RGB", self.size, data)

    def next(self):
        if self.proc is None:
            self._start()
        frame = self.read_frame()
        if frame is None:  # end of video: start over to loop
            self._start()
            frame = self.read_frame()
        return frame, self.delay

    def close(self):
        if self.proc:
            self.proc.kill()
            self.proc.stdout.close()
            self.proc.wait()
            self.proc = None


def prepare_preview(ffmpeg, path, kind, max_w, max_h):
    """Work out how to play the output, decoding it all up front if it fits.

    Runs on the worker thread so the UI never freezes. A 30 s, 60 fps 1080p
    clip is 1,800 full-HD frames (~11 GB decoded), so above the memory budget
    we stream frames from the file while playing instead.
    Returns (info dict, frame source).
    """
    if kind == "image":
        with Image.open(path) as im:
            w, h, n = im.width, im.height, getattr(im, "n_frames", 1)
        size = fit_size(w, h, max_w, max_h)
        info = dict(width=w, height=h, n_frames=n, display_size=size)
        if size[0] * size[1] * 3 * n > PREVIEW_MEMORY_BUDGET:
            return dict(info, streamed=True), ImageStream(path, size, n)
        frames, delays = [], []
        with Image.open(path) as im:
            for frame in ImageSequence.Iterator(im):
                frames.append(frame_for_display(frame, size))
                delays.append(frame.info.get("duration") or 100)
        return dict(info, streamed=False, length=sum(delays) / 1000), ListSource(frames, delays)

    dur, w, h, fps, _, _ = probe_video(ffmpeg, path)
    fps = fps or 30
    size = fit_size(w, h, max_w, max_h)
    n = round((dur or 0) * fps)
    info = dict(width=w, height=h, n_frames=n, display_size=size, length=dur)
    stream = VideoStream(ffmpeg, path, size, fps)
    if size[0] * size[1] * 3 * max(n, 1) > PREVIEW_MEMORY_BUDGET:
        return dict(info, streamed=True), stream
    stream._start()
    frames = []
    while (f := stream.read_frame()) is not None:
        frames.append(f)
    stream.close()
    if not frames:
        raise EncodeError("Couldn't decode the output for preview.")
    return (dict(info, n_frames=len(frames), streamed=False),
            ListSource(frames, [stream.delay] * len(frames)))


# ---- small formatting helpers ---------------------------------------------- #
def parse_time(text):
    """Accept '75', '75.5', '1:15', or '0:01:15.5' and return seconds."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Bad time: {text!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def fmt_time(secs):
    m, s = divmod(secs, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def fmt_size(n):
    return f"{n / 1048576:.2f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


class EncodeError(Exception):
    pass


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class GifMakerApp:
    def __init__(self, root):
        self.root = root
        root.title("GIF Maker")
        root.minsize(600, 0)
        self.ffmpeg, self.ffmpeg_desc = find_ffmpeg()
        self.encoders = available_encoders(self.ffmpeg) if self.ffmpeg else set()
        self.proc = None
        self.cancelled = False
        self.video_info = (None, None, None, None)
        self.has_audio = False
        self.temp_files = []
        self.preview_win = None
        self.ui_queue = queue.Queue()
        self._pump_ui()
        # Largest preview that fits on screen (room left for the info and buttons).
        # Read here because Tk may only be queried from the main thread.
        self.preview_max = (int(root.winfo_screenwidth() * 0.8),
                            int(root.winfo_screenheight() * 0.65))
        # Show unexpected errors in a dialog. By default Tk prints them to a
        # console, which is invisible when the script is double-clicked.
        root.report_callback_exception = self._report_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        home = os.path.expanduser("~")
        desktop = os.path.join(home, "Desktop")
        self.v = {
            "src": tk.StringVar(),
            "out_dir": tk.StringVar(value=desktop if os.path.isdir(desktop) else home),
            "out_name": tk.StringVar(),
            "format": tk.StringVar(value="GIF"),
            "whole": tk.BooleanVar(value=False),
            "start": tk.StringVar(value="0"),
            "end": tk.StringVar(),
            "max_len": tk.StringVar(value="10"),
            "fps": tk.IntVar(value=15),
            "res": tk.StringVar(value="480p"),
            "width": tk.StringVar(value="480"),
            "max_mb": tk.StringVar(value="8"),
            "speed": tk.DoubleVar(value=1.0),
            "colors": tk.IntVar(value=256),
            "dither": tk.StringVar(value=DITHER_MODES[0]),
            "quality": tk.IntVar(value=75),
            "audio": tk.BooleanVar(value=False),
            "loop": tk.StringVar(value="Forever"),
            "reverse": tk.BooleanVar(value=False),
            "open_after": tk.BooleanVar(value=True),
        }
        self._build_ui()
        # Keep labels live as the user changes settings.
        for key in ("res", "width"):
            self.v[key].trace_add("write", lambda *_: self._update_res_label())
        self.v["format"].trace_add("write", lambda *_: self._on_format_change())
        self._update_res_label()
        self._on_format_change()

        self.dnd = enable_drag_and_drop(root, self._on_drop)
        if self.dnd:
            self.info_lbl.config(text="No video selected — drag one here, or use Browse.")
        if self.ffmpeg:
            self.status.config(text=f"Ready — using {self.ffmpeg_desc}.")
        # Defer so the main window is drawn before any dialog appears.
        if not self.ffmpeg or Image is None:
            self.root.after(200, self.offer_install)

    # ---- dependency installer ------------------------------------------ #
    def offer_install(self):
        missing = []
        if not self.ffmpeg:
            missing.append(f"• FFmpeg — {self.ffmpeg_desc}")
        if Image is None:
            missing.append("• Pillow (needed for Preview)")
        if getattr(sys, "frozen", False):
            # The downloadable app bundles everything, and sys.executable is the app
            # itself, so "pip install" would just launch another copy of it.
            messagebox.showerror("Missing components",
                                 "This copy of GIF Maker is missing:\n\n" + "\n".join(missing) +
                                 "\n\nPlease download it again.")
            return
        if not messagebox.askyesno(
                "Install required components?",
                "GIF Maker needs:\n\n" + "\n".join(missing) +
                "\n\nInstall them now? (about 30–60 MB download)\n\n"
                f"This runs pip for the Python running this app:\n{sys.executable}"):
            if not self.ffmpeg:
                self.status.config(text="FFmpeg missing — nothing can be made until it's installed.")
            return
        self._set_busy(True)
        self.cancel_btn.config(state="disabled")
        self.status.config(text="Installing components… this can take a minute.")

        def work():
            try:
                ok, tail = install_requirements()
            except Exception as e:
                ok, tail = False, [str(e)]
            self.ui(lambda: self._install_done(ok, tail))
        threading.Thread(target=work, daemon=True).start()

    def _install_done(self, ok, tail):
        self._set_busy(False)
        load_pillow()
        self.ffmpeg, self.ffmpeg_desc = find_ffmpeg()
        self.encoders = available_encoders(self.ffmpeg) if self.ffmpeg else set()
        self._on_format_change()
        if ok and self.ffmpeg:
            self.status.config(text=f"Installed! Using {self.ffmpeg_desc}.")
            if self.v["src"].get():
                self._probe(self.v["src"].get())  # re-read the already chosen video
            if not self.dnd:  # tkinterdnd2 was just installed too
                self.dnd = enable_drag_and_drop(self.root, self._on_drop)
        else:
            self.status.config(text="Install failed — see the error message.")
            messagebox.showerror(
                "Install failed",
                "Couldn't install automatically:\n\n" + "\n".join(tail) +
                "\n\nTry running this in a terminal:\n"
                f'"{sys.executable}" -m pip install imageio-ffmpeg pillow tkinterdnd2')

    # ---- layout --------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        # Source
        f = ttk.LabelFrame(main, text="1. Video", padding=8)
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        ttk.Entry(f, textvariable=self.v["src"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(f, text="Browse…", command=self.pick_video).grid(row=0, column=1, padx=(6, 0))
        self.info_lbl = ttk.Label(f, text="No video selected", foreground="gray")
        self.info_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Clip range
        f = ttk.LabelFrame(main, text="2. Portion of video", padding=8)
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Checkbutton(f, text="Use whole video", variable=self.v["whole"],
                        command=self._toggle_range).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(f, text="Start").grid(row=1, column=0, sticky="w")
        self.start_e = ttk.Entry(f, textvariable=self.v["start"], width=10)
        self.start_e.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(f, text="End").grid(row=1, column=2, sticky="w")
        self.end_e = ttk.Entry(f, textvariable=self.v["end"], width=10)
        self.end_e.grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(f, text="Max output length (s)").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v["max_len"], width=10).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(f, text="Times: seconds (12.5) or m:ss (1:05). Blank max length = no limit.",
                  foreground="gray").grid(row=3, column=0, columnspan=4, sticky="w")

        # Format
        f = ttk.LabelFrame(main, text="3. Output format", padding=8)
        f.grid(row=2, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Format").grid(row=0, column=0, sticky="w", **pad)
        self.format_cb = ttk.Combobox(f, values=list(FORMATS), textvariable=self.v["format"],
                                      state="readonly", width=10)
        self.format_cb.grid(row=0, column=1, sticky="w", **pad)
        self.format_lbl = ttk.Label(f, text="", foreground="gray", wraplength=540, justify="left")
        self.format_lbl.grid(row=1, column=0, columnspan=2, sticky="w", **pad)

        # Resolution
        f = ttk.LabelFrame(main, text="4. Resolution", padding=8)
        f.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Size").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=RESOLUTIONS, textvariable=self.v["res"],
                     state="readonly", width=14).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="Custom width (px)").grid(row=0, column=2, sticky="w", **pad)
        self.width_e = ttk.Entry(f, textvariable=self.v["width"], width=8)
        self.width_e.grid(row=0, column=3, sticky="w", **pad)
        self.res_lbl = ttk.Label(f, text="", foreground="gray")
        self.res_lbl.grid(row=1, column=0, columnspan=4, sticky="w", **pad)

        # Settings (some only apply to certain formats; _on_format_change toggles them)
        f = ttk.LabelFrame(main, text="5. Settings", padding=8)
        f.grid(row=4, column=0, sticky="ew", **pad)
        self.colors_w = ttk.Spinbox(f, from_=2, to=256, textvariable=self.v["colors"], width=8)
        self.dither_w = ttk.Combobox(f, values=DITHER_MODES, textvariable=self.v["dither"],
                                     state="readonly", width=16)
        self.quality_w = ttk.Spinbox(f, from_=1, to=100, textvariable=self.v["quality"], width=8)
        self.loop_w = ttk.Combobox(f, values=LOOP_CHOICES, textvariable=self.v["loop"], width=16)
        rows = [
            ("Frame rate (fps)", ttk.Spinbox(f, from_=1, to=60, textvariable=self.v["fps"], width=8)),
            ("Max file size (MB, blank = none)", ttk.Entry(f, textvariable=self.v["max_mb"], width=10)),
            ("Playback speed (×)", ttk.Spinbox(f, from_=0.25, to=4.0, increment=0.25,
                                                textvariable=self.v["speed"], width=8)),
            ("Loop", self.loop_w),
            ("Colors (2–256, GIF)", self.colors_w),
            ("Dithering (GIF)", self.dither_w),
            ("Quality (1–100)", self.quality_w),
        ]
        for i, (label, widget) in enumerate(rows):
            r, c = divmod(i, 2)
            ttk.Label(f, text=label).grid(row=r, column=c * 2, sticky="w", **pad)
            widget.grid(row=r, column=c * 2 + 1, sticky="w", **pad)
        self.audio_w = ttk.Checkbutton(f, text="Keep sound (MP4/WebM)", variable=self.v["audio"])
        self.audio_w.grid(row=3, column=2, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(f, text="Reverse (play backwards; uses more memory on long clips)",
                        variable=self.v["reverse"]).grid(row=4, column=0, columnspan=4, sticky="w", **pad)

        # Output location
        f = ttk.LabelFrame(main, text="6. Save to", padding=8)
        f.grid(row=5, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v["out_dir"]).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Choose…", command=self.pick_folder).grid(row=0, column=2)
        ttk.Label(f, text="File name").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(f, textvariable=self.v["out_name"]).grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        ttk.Checkbutton(f, text="Open file when done",
                        variable=self.v["open_after"]).grid(row=2, column=1, sticky="w", pady=(4, 0))

        # Actions
        f = ttk.Frame(main)
        f.grid(row=6, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(f, mode="indeterminate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_btn = ttk.Button(f, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1)
        self.preview_btn = ttk.Button(f, text="Preview", command=lambda: self.start(preview=True))
        self.preview_btn.grid(row=0, column=2, padx=(6, 0))
        self.go_btn = ttk.Button(f, text="Generate", command=lambda: self.start(preview=False))
        self.go_btn.grid(row=0, column=3, padx=(6, 0))
        foot = ttk.Frame(main)  # the status line, with the support link on its right
        foot.grid(row=7, column=0, sticky="ew", **pad)
        foot.columnconfigure(0, weight=1)
        self.status = ttk.Label(foot, text="Ready.", wraplength=410)
        self.status.grid(row=0, column=0, sticky="w")
        support_link(foot).grid(row=0, column=1, sticky="ne", padx=(12, 0))

    # ---- small helpers -------------------------------------------------- #
    def _toggle_range(self):
        state = "disabled" if self.v["whole"].get() else "normal"
        self.start_e.config(state=state)
        self.end_e.config(state=state)

    def _fmt(self):
        return FORMATS[self.v["format"].get()]

    def _on_format_change(self):
        """Enable only the settings that apply to the chosen format, explain the
        format, and switch the file name's extension to match."""
        name = self.v["format"].get()
        fmt = FORMATS[name]
        missing = fmt["encoder"] and self.ffmpeg and fmt["encoder"] not in self.encoders
        self.format_lbl.config(
            text=(f"Your FFmpeg build can't make {name} files (missing {fmt['encoder']})."
                  if missing else fmt["about"]),
            foreground="red" if missing else "gray")
        is_gif, is_video = name == "GIF", fmt["kind"] == "video"
        self.colors_w.config(state="normal" if is_gif else "disabled")
        self.dither_w.config(state="readonly" if is_gif else "disabled")
        self.quality_w.config(state="disabled" if is_gif else "normal")
        self.loop_w.config(state="disabled" if is_video else "normal")
        self.audio_w.config(state="normal" if is_video else "disabled")

        out = self.v["out_name"].get()
        base, ext = os.path.splitext(out)
        if out and ext.lower().lstrip(".") in {f["ext"] for f in FORMATS.values()}:
            self.v["out_name"].set(f"{base}.{fmt['ext']}")

    def _update_res_label(self):
        """Show the exact output dimensions the current resolution choice gives."""
        custom = self.v["res"].get() == "Custom width"
        self.width_e.config(state="normal" if custom else "disabled")
        _, sw, sh, _ = self.video_info
        try:
            w = resolve_width(self.v["res"].get(), self.v["width"].get(), sw, sh)
        except ValueError:
            self.res_lbl.config(text="Enter a valid width.")
            return
        if sw and sh:
            ow = w or sw
            oh = round(sh * ow / sw / 2) * 2  # same nearest-even rounding FFmpeg's scale=W:-2 uses
            note = "  (source is smaller, kept original)" if (
                w is None and self.v["res"].get() not in ("Original", "Custom width")) else ""
            self.res_lbl.config(text=f"Output: {ow}×{oh} px{note}")
        else:
            self.res_lbl.config(text=f"Output width: {w or 'original'}"
                                     + (" px" if w else "") + "  (pick a video for exact size)")

    def pick_video(self):
        path = filedialog.askopenfilename(title="Choose a video", filetypes=VIDEO_TYPES)
        if path:
            self._use_video(path)

    def _use_video(self, path):
        self.v["src"].set(path)
        self.v["out_name"].set(os.path.splitext(os.path.basename(path))[0] + "." + self._fmt()["ext"])
        self._probe(path)

    def _on_drop(self, paths):
        """A video dropped on the window: use it like one picked with Browse."""
        if self.go_btn.instate(["disabled"]):
            self.status.config(text="Wait for this video to finish before choosing another.")
            return
        files = [p for p in paths if os.path.isfile(p)]
        if not files:
            self.status.config(text="Drop a video file (folders aren't supported).")
            return
        self._use_video(files[0])
        if len(paths) > 1:
            self.status.config(text=f"One video at a time: using {os.path.basename(files[0])}.")

    def _probe(self, path):
        """Read the video's length/size/fps and show it under the file box."""
        if not self.ffmpeg:
            self.info_lbl.config(text="FFmpeg isn't available yet — install it to read videos.",
                                 foreground="red")
        else:
            try:
                *info, self.has_audio, err = probe_video(self.ffmpeg, path)
            except OSError as e:  # FFmpeg binary couldn't be launched at all
                info, self.has_audio, err = (None, None, None, None), False, f"Couldn't run FFmpeg: {e}"
            self.video_info = tuple(info)
            dur, w, h, fps = self.video_info
            bits = []
            if dur: bits.append(f"Length {fmt_time(dur)}")
            if w: bits.append(f"{w}×{h}")
            if fps: bits.append(f"{fps:g} fps")
            if bits: bits.append("has sound" if self.has_audio else "no sound")
            self.info_lbl.config(text="  •  ".join(bits) or f"Couldn't read video: {err}",
                                 foreground="black" if bits else "red", wraplength=540)
            if dur:
                self.v["end"].set(f"{min(dur, 10):.2f}")
        self._update_res_label()

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose output folder", initialdir=self.v["out_dir"].get())
        if d:
            self.v["out_dir"].set(d)

    def set_status(self, text):
        """Safe to call from any thread."""
        self.ui(lambda: self.status.config(text=text))

    def ui(self, fn):
        """Run fn on the main (UI) thread. Safe to call from any thread.

        Tkinter isn't thread-safe: touching widgets, or even calling
        root.after(), from a worker thread works on some systems and silently
        does nothing on others. So workers only put callbacks in a queue, and
        the UI thread drains it every 50 ms in _pump_ui().
        """
        self.ui_queue.put(fn)

    def _pump_ui(self):
        # Reschedule FIRST: if a queued callback raises, the pump must keep
        # running, or every later message (status, preview, errors) is lost.
        self.root.after(50, self._pump_ui)
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                self._report_error(*sys.exc_info())

    def _dest_path(self, ext):
        out_dir = self.v["out_dir"].get().strip()
        if not os.path.isdir(out_dir):
            raise ValueError("Output folder doesn't exist.")
        name = self.v["out_name"].get().strip() or "output"
        base, cur = os.path.splitext(name)
        if cur.lower() != "." + ext:
            # Drop a known-but-wrong extension (e.g. .gif when saving MP4)
            name = (base if cur.lower().lstrip(".") in {f["ext"] for f in FORMATS.values()}
                    else name) + "." + ext
        return os.path.join(out_dir, name)

    def _confirm_overwrite(self, path):
        return not os.path.exists(path) or messagebox.askyesno(
            "Overwrite?", f"{os.path.basename(path)} already exists. Replace it?")

    # ---- validation ----------------------------------------------------- #
    def _collect(self):
        """Read and validate all inputs; raises ValueError with a friendly message."""
        src = self.v["src"].get().strip()
        if not os.path.isfile(src):
            raise ValueError("Choose a video file first.")
        name = self.v["format"].get()
        fmt = FORMATS[name]
        if fmt["encoder"] and fmt["encoder"] not in self.encoders:
            raise ValueError(f"Your FFmpeg build can't make {name} files. Pick another format.")

        dur, sw, sh, _ = self.video_info
        if self.v["whole"].get():
            start, end = 0.0, dur
        else:
            start = parse_time(self.v["start"].get()) or 0.0
            end = parse_time(self.v["end"].get())
            if end is None:
                end = dur
        if end is not None and end <= start:
            raise ValueError("End time must be after start time.")
        if dur and start >= dur:
            raise ValueError("Start time is past the end of the video.")
        clip_len = (end - start) if end is not None else 3600.0

        speed = float(self.v["speed"].get())
        if not 0.1 <= speed <= 10:
            raise ValueError("Speed must be between 0.1 and 10.")
        max_len = self.v["max_len"].get().strip()
        if max_len:
            # Max length is in output time, so at 2× speed we can read 2× as much source.
            clip_len = min(clip_len, float(max_len) * speed)

        quality = int(self.v["quality"].get())
        if not 1 <= quality <= 100:
            raise ValueError("Quality must be between 1 and 100.")
        width = resolve_width(self.v["res"].get(), self.v["width"].get(), sw, sh)
        max_mb = self.v["max_mb"].get().strip()
        loop_txt = self.v["loop"].get()
        loop = {"Forever": "forever", "Play once": "once"}.get(loop_txt) or int(loop_txt)

        return dict(src=src, fmt=name, ext=fmt["ext"], kind=fmt["kind"], encoder=fmt["encoder"],
                    start=start, duration=clip_len,
                    fps=max(1, int(self.v["fps"].get())), width=width, speed=speed,
                    colors=min(256, max(2, int(self.v["colors"].get()))),
                    dither=self.v["dither"].get(), quality=quality, loop=loop,
                    audio=fmt["kind"] == "video" and self.v["audio"].get() and self.has_audio,
                    reverse=self.v["reverse"].get(),
                    max_bytes=float(max_mb) * 1024 * 1024 if max_mb else None)

    # ---- generation ----------------------------------------------------- #
    def start(self, preview):
        if not self.ffmpeg or (preview and Image is None):
            self.offer_install()
            return
        try:
            opts = self._collect()
            if preview:
                # Encode to a temp file; the preview window can then save it
                # straight to the destination without re-encoding.
                fd, dst = tempfile.mkstemp(suffix="." + opts["ext"], prefix="gifmaker_preview_")
                os.close(fd)
                self.temp_files.append(dst)
            else:
                dst = self._dest_path(opts["ext"])
                if not self._confirm_overwrite(dst):
                    return
        except ValueError as e:
            messagebox.showwarning("Check settings", str(e))
            return
        opts["dst"] = dst
        self.cancelled = False
        self._set_busy(True)
        # Run FFmpeg off the UI thread so the window stays responsive.
        threading.Thread(target=self._worker, args=(opts, preview), daemon=True).start()

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.go_btn.config(state=state)
        self.preview_btn.config(state=state)
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.progress.start(12) if busy else self.progress.stop()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _run(self, cmd):
        """Run one FFmpeg command; raise Cancelled / EncodeError on failure."""
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True, errors="replace", **_POPEN_FLAGS)
        _, err = self.proc.communicate()
        if self.cancelled:
            raise Cancelled()
        if self.proc.returncode != 0:
            raise EncodeError("\n".join(err.strip().splitlines()[-4:]))

    def _encode(self, o):
        """Encode in the chosen format, retrying smaller until it fits max_bytes.

        Runs on the worker thread. Returns a dict describing the result.
        """
        width, fps = o["width"], o["fps"]
        if width is None and o["max_bytes"]:
            width = self.video_info[1] or 480  # need a concrete width so we can shrink it
        encode = {"GIF": self._encode_gif, "WebP": self._encode_webp}.get(o["fmt"], self._encode_video)
        size, met, width, fps, note = encode(o, width, fps)
        return dict(path=o["dst"], fmt=o["fmt"], ext=o["ext"], kind=o["kind"], size=size,
                    fps=fps, met=met, max_bytes=o["max_bytes"], note=note, audio=o["audio"],
                    shrunk=o["max_bytes"] is not None and (
                        width != (o["width"] or self.video_info[1]) or fps != o["fps"] or note))

    def _encode_gif(self, o, width, fps):
        # Pass 1: build the color palette once. Shrinking to meet the size
        # limit barely changes which colors appear, so every retry reuses it.
        fd, palette = tempfile.mkstemp(suffix=".png", prefix="gifmaker_palette_")
        os.close(fd)
        try:
            self.set_status("Analyzing colors…")
            self._run(build_palette_command(self.ffmpeg, o, palette, fps,
                                            width or self.video_info[1]))
            for attempt in range(1, MAX_PASSES + 1):
                self.set_status(f"Encoding GIF (pass {attempt}) — width {width or 'original'}"
                                f"{'px' if width else ''}, {fps} fps…")
                self._run(build_gif_command(self.ffmpeg, o, palette, fps, width))
                size, met, width, fps, again = self._shrink(o, width, fps)
                if not again:
                    return size, met, width, fps, ""
            return size, False, width, fps, ""
        finally:
            self._remove(palette)

    def _encode_webp(self, o, width, fps):
        quality, lowered = o["quality"], False
        for attempt in range(1, MAX_PASSES + 1):
            self.set_status(f"Encoding WebP (pass {attempt}) — width {width or 'original'}"
                            f"{'px' if width else ''}, {fps} fps, quality {quality}…")
            self._run(build_webp_command(self.ffmpeg, o, fps, width, quality, o["encoder"]))
            size = os.path.getsize(o["dst"])
            if not o["max_bytes"] or size <= o["max_bytes"]:
                break
            # Too big: lower quality first (cheap on looks, big on size), then shrink.
            if quality > MIN_WEBP_QUALITY:
                quality, lowered = max(MIN_WEBP_QUALITY, quality - 20), True
                continue
            size, met, width, fps, again = self._shrink(o, width, fps)
            if not again:
                break
        met = not o["max_bytes"] or size <= o["max_bytes"]
        return size, met, width, fps, (f"quality lowered to {quality}" if lowered else "")

    def _encode_video(self, o, width, fps):
        """MP4/WebM. Quality mode first; if over the limit, switch to a target
        bitrate computed from the limit. Video file size is basically
        bitrate × duration, so this lands close in one or two tries — much
        better than shrinking the picture."""
        name = o["fmt"]
        self.set_status(f"Encoding {name}…")
        self._run(build_video_command(self.ffmpeg, o, fps, width, o["quality"]))
        size = os.path.getsize(o["dst"])
        if not o["max_bytes"] or size <= o["max_bytes"]:
            return size, True, width, fps, ""

        out_dur = probe_video(self.ffmpeg, o["dst"])[0] or (o["duration"] / o["speed"])
        audio_bps = AUDIO[o["ext"]][1] if o["audio"] else 0
        # Leave ~8% for container overhead and encoder overshoot.
        bitrate = int(o["max_bytes"] * 8 * 0.92 / out_dur) - audio_bps
        for attempt in range(1, MAX_PASSES):
            if bitrate < 60_000:  # too few bits for this size: shrink the picture too
                if not width or width <= MIN_WIDTH:
                    break
                width = max(MIN_WIDTH, int(width * 0.7) // 2 * 2)
                bitrate = max(bitrate, 60_000)
            self.set_status(f"Fitting {name} to size (pass {attempt}) — "
                            f"{bitrate // 1000} kbps, width {width or 'original'}…")
            self._run(build_video_command(self.ffmpeg, o, fps, width, o["quality"], bitrate))
            size = os.path.getsize(o["dst"])
            if size <= o["max_bytes"]:
                return size, True, width, fps, f"bitrate set to {bitrate // 1000} kbps"
            bitrate = int(bitrate * o["max_bytes"] / size * 0.9)
        return size, False, width, fps, "lowest bitrate reached"

    def _shrink(self, o, width, fps):
        """Check the size; if over the limit, pick smaller settings to retry with.

        Returns (size, met_limit, width, fps, try_again).
        """
        size = os.path.getsize(o["dst"])
        if not o["max_bytes"] or size <= o["max_bytes"]:
            return size, True, width, fps, False
        # Too big: animated-image size scales roughly with pixel count × frame count.
        # Shrink width first (by sqrt of the ratio, since area ~ width²),
        # then start dropping fps once we hit the minimum width.
        ratio = o["max_bytes"] / size
        new_w = int(width * (ratio ** 0.5) * 0.95) // 2 * 2
        if new_w >= MIN_WIDTH:
            return size, False, new_w, fps, True
        if fps > MIN_FPS:
            return size, False, max(MIN_WIDTH, new_w), max(MIN_FPS, int(fps * max(ratio, 0.5))), True
        return size, False, width, fps, False  # already as small as we go

    def _worker(self, o, preview):
        try:
            result = self._encode(o)
        except Cancelled:
            self._remove(o["dst"])
            return self._finish("Cancelled.")
        except EncodeError as e:
            self._remove(o["dst"])
            return self._finish("FFmpeg failed.", error=str(e))
        except Exception as e:  # never leave the UI stuck in "busy" state
            return self._finish("Error.", error=f"{type(e).__name__}: {e}")

        note = ("" if result["met"] or not result["max_bytes"] else
                f"  — couldn't reach {fmt_size(result['max_bytes'])}; try a shorter clip or lower resolution")
        if preview:
            # Decode frames here, on the worker thread, so the window never freezes.
            self.set_status("Loading preview…")
            try:
                result["preview"] = prepare_preview(self.ffmpeg, o["dst"], o["kind"],
                                                    *self.preview_max)
            except Exception as e:
                return self._finish("Couldn't load the preview.", error=f"{type(e).__name__}: {e}")
            self._finish(f"Preview ready: {fmt_size(result['size'])}{note}", preview=result)
        else:
            self._finish(f"Done! {os.path.basename(o['dst'])} — {fmt_size(result['size'])}{note}",
                         path=o["dst"])

    def _finish(self, msg, path=None, error=None, preview=None):
        """Hand results back to the UI thread (Tk isn't thread-safe)."""
        def run():
            self._set_busy(False)
            self.status.config(text=msg)
            if error:
                log_error(f"{msg}\n{error}")
                messagebox.showerror("Error", f"{msg}\n\n{error}")
            elif preview:
                if self.preview_win:  # only one preview at a time
                    self.preview_win.close()
                try:
                    self.preview_win = PreviewWindow(self, preview)
                except Exception:
                    import traceback
                    self.status.config(text="Preview failed to open — see the error message.")
                    messagebox.showerror("Preview failed", traceback.format_exc()[-1500:])
                    log_error(traceback.format_exc())
            elif path and self.v["open_after"].get():
                open_file(path)
        self.ui(run)

    def _report_error(self, exc, val, tb):
        import traceback
        self._set_busy(False)
        detail = "".join(traceback.format_exception(exc, val, tb))
        log_error(detail)
        self.status.config(text=f"Unexpected error: {val}")
        messagebox.showerror("Unexpected error", detail[-1500:])

    def _remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def on_close(self):
        self.cancel()
        if self.preview_win:
            self.preview_win.close()
        for p in self.temp_files:
            self._remove(p)
        self.root.destroy()


# --------------------------------------------------------------------------- #
#  Preview window
# --------------------------------------------------------------------------- #
class PreviewWindow:
    """Plays the encoded file and shows its stats; can save it without re-encoding.

    Frames are shown through ONE reusable PhotoImage (paste() replaces its
    pixels), so on-screen memory stays at a single frame however long the clip.
    """

    def __init__(self, app, result):
        self.app, self.result = app, result
        p, self.source = result["preview"]
        self.after_id = None
        self.playing = True
        self.closed = False

        self.win = tk.Toplevel(app.root)
        self.win.title(f"{result['fmt']} Preview")
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(self.win, padding=10)
        body.pack(fill="both", expand=True)
        self.photo = ImageTk.PhotoImage("RGB", p["display_size"])
        tk.Label(body, image=self.photo, bd=0).pack()

        size_txt = fmt_size(result["size"])
        if result["max_bytes"]:
            size_txt += (f"  (limit {fmt_size(result['max_bytes'])} ✓)" if result["met"]
                         else f"  (over the {fmt_size(result['max_bytes'])} limit)")
        length = p.get("length") or p["n_frames"] / result["fps"]
        info = (f"{result['fmt']}  •  File size: {size_txt}\n"
                f"Dimensions: {p['width']}×{p['height']} px   •   {p['n_frames']} frames   •   "
                f"{result['fps']} fps   •   {length:.1f} s")
        if result["shrunk"]:
            info += "\nSettings were reduced automatically to fit the size limit"
            info += f" ({result['note']})." if result["note"] else "."
        if p["display_size"] != (p["width"], p["height"]):
            info += f"\nShown at {p['display_size'][0]}×{p['display_size'][1]} to fit your screen."
        if p["streamed"]:
            info += "\nLarge file: preview may play slower than the real thing."
        if result["audio"]:
            info += "\nThe preview is silent; the saved file keeps the sound."
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        ttk.Label(body, text=info, justify="center", font=bold).pack(pady=(8, 4))

        btns = ttk.Frame(body)
        btns.pack(pady=(4, 0))
        self.play_btn = ttk.Button(btns, text="Pause", command=self.toggle)
        self.play_btn.pack(side="left", padx=4)
        ttk.Button(btns, text=f"Save this {result['fmt']}", command=self.save).pack(side="left", padx=4)
        ttk.Button(btns, text="Close", command=self.close).pack(side="left", padx=4)

        # Open centered over the main window. Left to itself, Windows puts a new
        # window at the top-left of the PRIMARY monitor, so with several monitors
        # it can open on a different screen from the app. And a transient window
        # has no taskbar button to find it by. Withdrawn first so it never flashes
        # at the default spot while we measure it. The first frame goes in first:
        # until then the PhotoImage is 0×0 and the window measures far too small.
        self.win.withdraw()
        self.tick()
        self.win.update_idletasks()
        root = app.root
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = max(root.winfo_rooty(), root.winfo_rooty() + (root.winfo_height() - h) // 2)
        self.win.geometry(f"+{x}+{y}")  # "+-1500+100" is valid: monitors left of the primary
        self.win.deiconify()

        # Make sure the window appears in front of the main window, not behind it.
        self.win.transient(app.root)
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(300, lambda: self.win.attributes("-topmost", False))
        self.win.focus_force()

    def tick(self):
        """Show one frame, then schedule the next using that frame's own delay
        (minus the time spent decoding it, so streaming stays close to real speed)."""
        if self.closed:
            return
        t0 = time.perf_counter()
        frame, delay = self.source.next()
        if frame is not None:
            self.photo.paste(frame)
        if self.playing:
            spent = int((time.perf_counter() - t0) * 1000)
            self.after_id = self.win.after(max(1, delay - spent), self.tick)

    def toggle(self):
        self.playing = not self.playing
        self.play_btn.config(text="Pause" if self.playing else "Play")
        if self.playing:
            self.tick()
        elif self.after_id:
            self.win.after_cancel(self.after_id)

    def save(self):
        try:
            dst = self.app._dest_path(self.result["ext"])
        except ValueError as e:
            messagebox.showwarning("Check settings", str(e), parent=self.win)
            return
        if os.path.exists(dst) and not messagebox.askyesno(
                "Overwrite?", f"{os.path.basename(dst)} already exists. Replace it?", parent=self.win):
            return
        shutil.copyfile(self.result["path"], dst)  # same bytes as previewed, no re-encode
        self.app.status.config(text=f"Saved {os.path.basename(dst)} — {fmt_size(self.result['size'])}")
        self.close()
        if self.app.v["open_after"].get():
            open_file(dst)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.after_id:
            self.win.after_cancel(self.after_id)
        self.source.close()  # release the file before deleting it (Windows locks open files)
        self.win.destroy()
        self.app._remove(self.result["path"])
        if self.app.preview_win is self:
            self.app.preview_win = None


def enable_drag_and_drop(root, on_drop, on_enter=None, on_leave=None):
    """Accept files dragged onto the window, using tkinterdnd2 if it's installed.
    Returns False when it isn't (or its native part can't load): the Browse
    buttons still work, so drag and drop is optional.

    on_drop gets the list of dropped paths. Every widget is registered, so a
    drop works anywhere in the window.
    """
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        TkinterDnD.require(root)
    except Exception:
        return False

    def drop(event):
        # The paths come as a Tcl list: names with spaces are in {braces}.
        on_drop(list(root.tk.splitlist(event.data)))
        return event.action  # "copy": the files stay where they are

    def hook(fn):
        def handler(event):
            if fn:
                fn()
            return event.action
        return handler

    widgets, todo = [], list(root.winfo_children())
    while todo:
        w = todo.pop()
        widgets.append(w)
        todo += w.winfo_children()
    for w in widgets:
        w.drop_target_register(DND_FILES)
        w.dnd_bind("<<Drop>>", drop)
        w.dnd_bind("<<DropEnter>>", hook(on_enter))
        w.dnd_bind("<<DropLeave>>", hook(on_leave))
    return True


LOG_PATH = os.path.join(tempfile.gettempdir(), "gif_maker_errors.log")


def log_error(text):
    """Append an error to a log file. Errors can otherwise vanish when the
    script runs without a console (double-clicked / pythonw)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
    except OSError:
        pass


def open_file(path):
    """Open a file with the OS default app."""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def self_test():
    """Used by the release build: exit code 0 if FFmpeg and drag and drop both
    work in the packaged app, so a missing piece fails the build."""
    if not find_ffmpeg()[0]:
        return 1
    try:
        root = tk.Tk()
        root.withdraw()
        from tkinterdnd2 import TkinterDnD
        TkinterDnD.require(root)
        root.destroy()
    except Exception as e:
        log_error(f"Self-test failed: {type(e).__name__}: {e}")
        return 2
    return 0


SUPPORT_URL = "https://ko-fi.com/collinbrooksdev"


def support_link(parent, bg=None, font=None):
    """A small "Support this project" link that opens the Ko-fi page in the browser."""
    import webbrowser
    kw = {"bg": bg} if bg else {}
    link = tk.Label(parent, text="\u2665 Support this project", fg="#c8364f", cursor="hand2",
                    font=font or ("", 9, "underline"), **kw)
    link.bind("<Button-1>", lambda e: webbrowser.open(SUPPORT_URL))
    return link


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    root = tk.Tk()
    try:  # nicer native look where available
        ttk.Style().theme_use("vista" if sys.platform == "win32" else
                              "aqua" if sys.platform == "darwin" else "clam")
    except tk.TclError:
        pass
    GifMakerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
