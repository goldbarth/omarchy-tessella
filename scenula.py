#!/usr/bin/env python3
"""Scenula backend: sync artists, tag images, render triptychs.

Commands:
  init      prepare data aliases and files before shell watchers start
  migrate   migrate an updated Tessella checkout and its shell configuration
  sync      fetch every configured artist's RSS feed, download new images,
            drop removed artists, analyze image colors, tag untagged images via Ollama
  cancel    stop a running sync; what is done so far is kept
  next      pick a layout for the screen (e.g. 3 portraits on 21:9, 2 portraits
            or 1 landscape on 16:9), 1-2 themes and images, preferring images
            that fit the selected palette; render and set it
  rerender  render the current selection again (new theme colors); picks a
            fresh one if the layout no longer suits the screen
            --if-changed: only act when the screen size differs
  back      show the previous selection again; the current one becomes the previous
  replace <artist/id>
            swap one image of the current collage for another that fits its slot,
            themes and palette; the other images stay
  status    print a JSON summary for the shell panel

Files (single writer each):
  config   ~/.config/omarchy/scenula/config.json   written by the panel
  library  ~/.local/share/scenula/library.json          written by sync
  sync     ~/.local/state/scenula/sync.json             written by sync
  state    ~/.local/state/scenula/state.json            written by next/rerender/back/replace
  palettes palettes.json next to this script               shipped catalog, read only
"""

import base64
import concurrent.futures
import fcntl
import html
import json
import math
import os
import random
import re
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = HOME / ".config/omarchy/scenula/config.json"
DATA_DIR = HOME / ".local/share/scenula"
STATE_DIR = HOME / ".local/state/scenula"
LIBRARY_PATH = DATA_DIR / "library.json"
IMAGES_DIR = DATA_DIR / "images"
COLLAGE_DIR = DATA_DIR / "collages"
SYNC_PATH = STATE_DIR / "sync.json"
STATE_PATH = STATE_DIR / "state.json"
THEME_COLORS = HOME / ".local/state/omarchy/current/theme/colors.toml"
PALETTES_PATH = Path(__file__).resolve().with_name("palettes.json")

OLD_ID = "goldbarth.tessella"
PLUGIN_ID = "goldbarth.scenula"


@contextmanager
def migration_lock():
    # Outside either state directory, so concurrent first starts share one lock.
    parent = HOME / ".local/state"
    parent.mkdir(parents=True, exist_ok=True)
    with open(parent / "scenula-migration.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def data_paths():
    return [(HOME / parent / "tessella", HOME / parent / "scenula")
            for parent in (".config/omarchy", ".local/share", ".local/state")]


def check_data_paths():
    for old, new in data_paths():
        for path in (old, new):
            if os.path.lexists(path) and not path.is_dir():
                raise RuntimeError(f"Not a readable directory: {path}")
        if old.exists() and new.exists() and not old.samefile(new):
            raise RuntimeError(f"Both {old} and {new} exist independently; "
                               "resolve this conflict before migrating. Nothing was merged or removed.")


def prepare_data_paths():
    # Keep legacy directories, absolute metadata paths, wallpaper links and lock
    # inodes intact. Scenula uses aliases rather than copying or rewriting data.
    for old, new in data_paths():
        if new.exists():
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        if old.exists():
            new.symlink_to(old, target_is_directory=True)
        else:
            new.mkdir()


def prepare_data():
    with migration_lock():
        check_data_paths()
        prepare_data_paths()


def cmd_init():
    # FileView watchers need existing files before configuration is loaded.
    for path in (CONFIG_PATH, STATE_PATH, SYNC_PATH):
        with open(path, "a"):
            pass


def migrated_shell_config(config):
    """Change only plugin identity fields, preserving placement and options."""
    def entries(items):
        if not isinstance(items, list):
            raise RuntimeError("Invalid shell.json plugin list")
        result = []
        found = False
        for entry in items:
            identity = entry.get("id") if isinstance(entry, dict) else entry
            if identity in (OLD_ID, PLUGIN_ID):
                if found:
                    raise RuntimeError("Duplicate Tessella/Scenula entries in shell.json; "
                                       "resolve their placement/options before migrating")
                found = True
                if isinstance(entry, dict):
                    entry = dict(entry, id=PLUGIN_ID)
                else:
                    entry = PLUGIN_ID
            result.append(entry)
        return result

    config = json.loads(json.dumps(config))
    if not isinstance(config, dict) or config.get("version") != 1:
        raise RuntimeError("Expected shell.json version 1")
    bar = config.get("bar", {})
    if not isinstance(bar, dict):
        raise RuntimeError("Invalid shell.json bar object")
    layout = bar.get("layout", {})
    if not isinstance(layout, dict):
        raise RuntimeError("Invalid shell.json bar layout")
    widget_count = 0
    for section, items in layout.items():
        if not isinstance(items, list):
            raise RuntimeError(f"Invalid shell.json bar section: {section}")
        widget_count += sum((entry.get("id") if isinstance(entry, dict) else entry)
                            in (OLD_ID, PLUGIN_ID) for entry in items)
        layout[section] = entries(items)
    if widget_count > 1:
        raise RuntimeError("Tessella/Scenula occurs in multiple bar sections; resolve before migrating")
    if bar.get("centerAnchor") == OLD_ID:
        bar["centerAnchor"] = PLUGIN_ID
    for key in ("plugins", "disabledPlugins"):
        if key in config:
            config[key] = entries(config[key])
    return config


def cmd_migrate():
    plugins = HOME / ".config/omarchy/plugins"
    old, new = plugins / OLD_ID, plugins / PLUGIN_ID
    shell_path = HOME / ".config/omarchy/shell.json"
    with migration_lock():
        # Preflight every conflict before modifying user configuration or paths.
        check_data_paths()
        if os.path.lexists(old) and os.path.lexists(new):
            raise RuntimeError(f"Both {old} and {new} exist; keep one installation before migrating. "
                               "Neither installation was changed.")
        installed = old if os.path.lexists(old) else new
        if installed.is_symlink() or not installed.is_dir():
            raise RuntimeError("Run migrate from a regular installed checkout, not a development symlink")
        if installed.resolve() != Path(__file__).resolve().parent:
            raise RuntimeError(f"Run the installed script: python3 {installed}/scenula.py migrate")
        if read_json(installed / "manifest.json", {}).get("id") != PLUGIN_ID:
            raise RuntimeError("Update the installed checkout to Scenula before migrating")
        raw = shell_path.read_bytes() if shell_path.exists() else None
        config = json.loads(raw) if raw is not None else None
        updated = migrated_shell_config(config) if raw is not None else None
        changed = updated != config
        backup = shell_path.with_name("shell.json.before-scenula")
        if changed:
            # Never overwrite an earlier backup, including after an interrupted run.
            try:
                with open(backup, "xb") as f:
                    f.write(raw)
                shutil.copymode(shell_path, backup)
            except FileExistsError:
                pass
        prepare_data_paths()
        if installed == old:
            old.rename(new)
        try:
            if changed:
                # Refuse an edit made while preflight was running.
                if shell_path.read_bytes() != raw:
                    raise RuntimeError("shell.json changed during migration; retry")
                write_json(shell_path.resolve(), updated, shell_path.stat().st_mode & 0o777)
        except BaseException:
            if installed == old:
                new.rename(old)
            raise
        # GitHub redirects keep old remotes working. Update only known upstream
        # URLs; forks and custom transports remain under the user's control.
        if (new / ".git").is_dir():
            remote = subprocess.run(["git", "-C", str(new), "remote", "get-url", "origin"],
                                    capture_output=True, text=True)
            url = remote.stdout.strip()
            if remote.returncode == 0 and re.fullmatch(
                    r"(?:https://github\.com/|git@github\.com:)goldbarth/omarchy-tessella(?:\.git)?", url):
                result = subprocess.run(["git", "-C", str(new), "remote", "set-url", "origin",
                                         url.replace("omarchy-tessella", "omarchy-scenula")],
                                        capture_output=True, text=True)
                if result.returncode:
                    print("Could not update origin; GitHub's rename redirect is still usable.", file=sys.stderr)
    print(f"Scenula installation: {new}")
    if changed:
        print(f"Shell configuration backup: {backup}")
    print("Existing Tessella data directories were kept in place; Scenula paths alias them.")
    try:
        result = subprocess.run(["omarchy-shell", "shell", "rescanPlugins"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        print("Migration saved. Start the shell, then run: omarchy-shell shell rescanPlugins",
              file=sys.stderr)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
IMAGE_RE = re.compile(
    r'<img src="(https://cdn[a-z0-9]*\.artstation\.com/p/assets/images/images/(\d+)/(\d+)/(\d+)/[^"/]+/[^"]+?\.(jpg|jpeg|png|webp))(?:\?\d+)?"')

DEFAULT_CONFIG = {
    "enabled": False,  # replaces the theme background, so only after the user opts in
    "intervalMinutes": 30,
    "artists": [],
    "disabledThemes": [],
    "gap": 10,
    "visionModel": "gemma3:12b",
    "palette": "",
    # "artist/id" keys the user hid. Lives in the config, not the sync-owned
    # library, so syncing and re-tagging never touch it.
    "hiddenImages": [],
}

# Layouts are derived from the screen's aspect ratio: as many typical portrait
# (4:5) or landscape (16:9) images as fit side by side, or stacked on a portrait
# screen. A layout qualifies if a typical image loses at most LAYOUT_TOLERANCE
# to cropping; a single image may lose at most MAX_CROP to fit its slot.
IMAGE_KINDS = {"portrait": 0.8, "landscape": 16 / 9}
LAYOUT_TOLERANCE = 0.2
MAX_CROP = 0.3
# A theme covering more of the pool than this is too generic to stand alone.
GENERIC_SHARE = 0.6
PAIR_CHANCE = 0.6

# Every image is reduced to a few dominant colors (CIELAB + share) during sync.
# Palette matching compares those to a palette, so changing or adding palettes
# never needs a re-analysis. Bump SWATCH_VERSION when image_swatches() changes.
SWATCH_VERSION = 1
SWATCH_COUNT = 8
# A close palette match is among the best PALETTE_SHARE of the analyzed pool and
# at most PALETTE_MAX_SCORE (mean CIELAB distance) away. If close matches leave
# fewer than PALETTE_VARIETY collages' worth of projects, the best RELAXED_SHARE
# is used instead, and without enough of those the palette is ignored.
PALETTE_SHARE = 0.15
PALETTE_MAX_SCORE = 10
RELAXED_SHARE = 0.3
PALETTE_VARIETY = 3

# Bump TAG_VERSION whenever VOCAB or the prompt changes: the next sync re-tags everything.
TAG_VERSION = 4
VOCAB = {
    "setting": ["snow", "desert", "ocean", "underwater", "space", "city", "mountains", "forest",
                "jungle", "sky", "interior", "industrial", "coast", "ruins", "wasteland"],
    "subject": ["megastructure", "spaceship", "vehicle", "mech", "robot", "tower", "station",
                "street", "character", "creature", "landscape"],
    "mood": ["serene", "epic", "eerie", "playful", "melancholic", "dramatic", "cozy", "lonely"],
    "light": ["day", "night", "sunset", "neon"],
    "color": ["white", "blue", "teal", "green", "yellow", "orange", "red", "pink", "purple",
              "gray", "dark"],
}
TAG_GROUP = {tag: group for group, tags in VOCAB.items() for tag in tags}

# Describing first makes the model look at the image before picking tags,
# which noticeably reduces default answers.
TAG_PROMPT = (
    "You tag artworks for a wallpaper picker that groups similar images. "
    "First write a one-sentence description of what is actually shown. "
    "Then pick tags ONLY from these options, only tags that clearly fit this specific image "
    "(setting/subject/mood: 1-2 values, light: 1, color: the 1-2 dominant colors):\n"
    + "\n".join(f"{group}: {', '.join(tags)}" for group, tags in VOCAB.items())
    + '\nReply JSON only: {"description": "", '
    + ", ".join(f'"{group}": []' for group in VOCAB) + "}"
)


# ---------- small helpers ----------

def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_config():
    config = dict(DEFAULT_CONFIG)
    config.update(read_json(CONFIG_PATH, {}))
    if not isinstance(config["hiddenImages"], list):
        config["hiddenImages"] = []
    config["artists"] = [normalize_artist(a) for a in config.get("artists", []) if normalize_artist(a)]
    return config


def normalize_artist(value):
    """Accept a username, a profile URL or a <name>.artstation.com URL."""
    value = str(value or "").strip().lower()
    match = re.match(r"^(?:https?://)?([a-z0-9_-]+)\.artstation\.com", value)
    if match and match.group(1) != "www":
        return match.group(1)
    value = re.sub(r"^(?:https?://)?(?:www\.)?artstation\.com/", "", value)
    value = value.split("/")[0].split("?")[0]
    return value if re.fullmatch(r"[a-z0-9_-]+", value) else ""


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class Lock:
    """Non-blocking exclusive lock; a second sync/render exits instead of piling up."""

    def __init__(self, name):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.file = open(STATE_DIR / f"{name}.lock", "w")

    def __enter__(self):
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("already running", file=sys.stderr)
            sys.exit(3)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


# ---------- sync ----------

class SyncProgress:
    def __init__(self):
        self.data = read_json(SYNC_PATH, {})
        self.data.update({"running": True, "pid": os.getpid(), "phase": "start",
                          "done": 0, "total": 0, "errors": []})
        self.save()

    def set(self, **fields):
        self.data.update(fields)
        self.save()

    def error(self, message):
        self.data["errors"].append(message)
        self.save()

    def save(self):
        write_json(SYNC_PATH, self.data)


def fetch_artist_feed(artist):
    """All projects of an artist across RSS pages: [{link, title, images: [(id, url, ext)]}]."""
    projects = []
    for page in range(1, 100):
        raw = http_get(f"https://www.artstation.com/{artist}.rss?page={page}")
        channel = ET.fromstring(raw).find("channel")
        items = channel.findall("item") if channel is not None else []
        if not items:
            break
        for item in items:
            content = item.findtext(CONTENT_NS) or ""
            seen, images = set(), []
            for url, a, b, c, ext in IMAGE_RE.findall(content):
                asset_id = a + b + c
                if asset_id not in seen:
                    seen.add(asset_id)
                    images.append((asset_id, html.unescape(url), ext))
            title = re.sub(r"\s+by\s+[^|]+$", "", item.findtext("title") or "").strip()
            projects.append({"link": item.findtext("link") or "", "title": title, "images": images})
    return projects


def image_size(path):
    out = subprocess.run(["magick", "identify", "-format", "%w %h\n", str(path)],
                         capture_output=True, text=True, check=True).stdout.split("\n")[0]
    w, h = out.split()
    return int(w), int(h)


def run_parallel(func, items, workers=4):
    """Yield (item, result, error) as calls finish. On cancel, queued calls are dropped."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(func, item): item for item in items}
        for future in concurrent.futures.as_completed(futures):
            try:
                yield futures[future], future.result(), None
            except Exception as e:
                yield futures[future], None, e
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def download_entry(entry):
    """Download one image and record its size. Mutates entry."""
    path = IMAGES_DIR / entry["artist"] / f"{entry['id']}.{entry['ext']}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(http_get(entry["url"], timeout=60))
    entry["w"], entry["h"] = image_size(tmp)
    os.replace(tmp, path)
    entry["path"] = str(path)
    return entry


def hex_to_lab(value):
    """sRGB hex (#rrggbb) to CIELAB (D65)."""
    return rgb_to_lab(*(int(value[i:i + 2], 16) for i in (1, 3, 5)))


def rgb_to_lab(r, g, b):
    def linear(c):
        c /= 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    r, g, b = linear(r), linear(g), linear(b)
    x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047)
    y = f(0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    return 116 * y - 16, 500 * (x - y), 200 * (y - z)


HISTOGRAM_RE = re.compile(r"^\s*(\d+):\s*\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", re.M)


def image_swatches(path):
    """The image's SWATCH_COUNT dominant colors as [[L, a, b, share], ...]."""
    out = subprocess.run(["magick", "-define", "jpeg:size=128x128", str(path), "-alpha", "off",
                          "-colorspace", "sRGB", "-depth", "8", "-resize", "64x64!", "+dither",
                          "-colors", str(SWATCH_COUNT), "-format", "%c", "histogram:info:-"],
                         capture_output=True, text=True, check=True).stdout
    counts = [(int(n), rgb_to_lab(float(r), float(g), float(b)))
              for n, r, g, b in HISTOGRAM_RE.findall(out)]
    total = sum(n for n, _ in counts)
    if not total:
        raise ValueError("no colors found")
    return [[round(L, 1) + 0.0, round(a, 1) + 0.0, round(b, 1) + 0.0, round(n / total, 3)]
            for n, (L, a, b) in sorted(counts, key=lambda c: -c[0])]


def ollama_url():
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    return host if host.startswith("http") else f"http://{host}"


def tag_image(path, model):
    jpg = subprocess.run(["magick", path, "-resize", "768x768>", "-quality", "90", "jpg:-"],
                         capture_output=True, check=True).stdout
    body = {"model": model, "prompt": TAG_PROMPT, "images": [base64.b64encode(jpg).decode()],
            "stream": False, "format": "json", "keep_alive": "2m",
            "options": {"temperature": 0}}
    req = urllib.request.Request(f"{ollama_url()}/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                answer = json.loads(json.loads(resp.read())["response"])
            break
        except urllib.error.HTTPError:
            # Ollama answers 400/500 transiently while (un)loading a model.
            if attempt == 2:
                raise
            time.sleep(3)
    tags = []
    for group in VOCAB:
        values = answer.get(group) or []
        if isinstance(values, str):
            values = [values]
        for value in values[:2]:
            value = str(value).strip().lower()
            if TAG_GROUP.get(value) == group and value not in tags:
                tags.append(value)
    return tags


class Cancelled(BaseException):
    """Raised in the main thread on SIGTERM/SIGINT. A BaseException, so the
    per-image `except Exception` handlers do not swallow it."""


def raise_cancelled(signum, frame):
    raise Cancelled()


def cmd_sync():
    with Lock("sync"):
        progress = SyncProgress()
        try:
            signal.signal(signal.SIGTERM, raise_cancelled)
            signal.signal(signal.SIGINT, raise_cancelled)
            run_sync(progress)
        except Cancelled:
            # Counts as a sync, so the daily auto-sync does not restart the work
            # the user just stopped. "Sync now" picks up where it ended.
            progress.set(phase="cancelled", lastSync=int(time.time()))
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            # Never leave the panel showing a sync that is no longer running.
            progress.set(running=False)


def running_sync_pid():
    """PID of the running sync, or None. sync.json can claim a sync that was
    killed hard (SIGKILL, crash), so check the process itself."""
    pid = read_json(SYNC_PATH, {}).get("pid")
    if not isinstance(pid, int):
        return None
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    except OSError:
        return None
    if len(args) >= 2 and args[-1] == b"sync" and Path(os.fsdecode(args[-2])).name in ("scenula.py", "tessella.py"):
        return pid
    return None


def cmd_cancel():
    pid = running_sync_pid()
    if pid is None:
        print("no sync running", file=sys.stderr)
        return
    os.kill(pid, signal.SIGTERM)


def run_sync(progress):
    config = load_config()
    library = read_json(LIBRARY_PATH, {"images": {}})
    images = library["images"]

    # Drop downloads an interrupted sync left unfinished, and artists that
    # were removed in the panel (including their files).
    for key in [k for k, e in images.items()
                if e.get("new") or e["artist"] not in config["artists"]]:
        del images[key]
    if IMAGES_DIR.exists():
        for folder in IMAGES_DIR.iterdir():
            if folder.is_dir() and folder.name not in config["artists"]:
                for f in folder.iterdir():
                    f.unlink()
                folder.rmdir()

    # Fetch feeds and collect new images.
    artist_errors = {}
    pending = []
    for i, artist in enumerate(config["artists"]):
        progress.set(phase="feeds", done=i, total=len(config["artists"]), artist=artist)
        try:
            projects = fetch_artist_feed(artist)
        except urllib.error.HTTPError as e:
            artist_errors[artist] = "not found" if e.code == 404 else f"HTTP {e.code}"
            continue
        except (urllib.error.URLError, ET.ParseError, TimeoutError) as e:
            artist_errors[artist] = f"feed error: {e}"
            continue
        if not projects:
            artist_errors[artist] = "no projects found"
        for project in projects:
            for asset_id, url, ext in project["images"]:
                key = f"{artist}/{asset_id}"
                if key not in images:
                    images[key] = {"id": asset_id, "artist": artist, "url": url, "ext": ext,
                                   "project": project["link"], "title": project["title"],
                                   "path": None, "w": 0, "h": 0, "tags": None, "new": True}
                    pending.append(images[key])
    for artist, message in artist_errors.items():
        progress.error(f"{artist}: {message}")
    # Known images without a file: earlier versions skipped landscapes, or a
    # download failed. Fetch them (again).
    pending += [e for e in images.values() if not e.get("path") and not e.get("new")]

    # Download new images, a few in parallel.
    progress.set(phase="download", done=0, total=len(pending))
    for n, (entry, _, error) in enumerate(run_parallel(download_entry, pending), 1):
        if error is None:
            entry.pop("new", None)
        else:  # keep going; the image is retried next sync
            if entry.get("new"):
                del images[f"{entry['artist']}/{entry['id']}"]
            progress.error(f"Download {entry['url']}: {error}")
        if n % 10 == 0 or n == len(pending):
            progress.set(done=n)
            write_json(LIBRARY_PATH, library)
    write_json(LIBRARY_PATH, library)

    # Reduce new images to their dominant colors for palette matching. Fast
    # (a few seconds per thousand images), so it runs before tagging.
    unanalyzed = [e for e in images.values()
                  if e.get("path") and e.get("swatchVersion") != SWATCH_VERSION]
    progress.set(phase="colors", done=0, total=len(unanalyzed))
    for n, (entry, swatches, error) in enumerate(
            run_parallel(lambda e: image_swatches(e["path"]), unanalyzed), 1):
        if error is None:
            entry["swatches"] = swatches
            entry["swatchVersion"] = SWATCH_VERSION
        else:
            progress.error(f"Colors {entry['id']}: {error}")
        if n % 100 == 0 or n == len(unanalyzed):
            progress.set(done=n)
            write_json(LIBRARY_PATH, library)
    write_json(LIBRARY_PATH, library)

    # Tag downloaded images that have no (current) tags yet.
    untagged = [e for e in images.values()
                if e.get("path") and (e.get("tags") is None or e.get("tagVersion") != TAG_VERSION)]
    progress.set(phase="tagging", done=0, total=len(untagged))
    for n, entry in enumerate(untagged, 1):
        try:
            entry["tags"] = tag_image(entry["path"], config["visionModel"])
            entry["tagVersion"] = TAG_VERSION
        except urllib.error.HTTPError as e:
            progress.error(f"Tagging {entry['id']}: Ollama HTTP {e.code}")
        except urllib.error.URLError as e:
            progress.error(f"Ollama unreachable ({e.reason}); {len(untagged) - n + 1} images left untagged")
            break
        except Exception as e:
            progress.error(f"Tagging {entry['id']}: {e}")
        if n % 5 == 0 or n == len(untagged):
            progress.set(done=n)
            write_json(LIBRARY_PATH, library)
    write_json(LIBRARY_PATH, library)

    progress.set(running=False, phase="done", lastSync=int(time.time()),
                 artistErrors=artist_errors)


# ---------- selection ----------

# Covers and posters carry big title typography that looks cheap when cropped.
# The vision model cannot tell that apart from signs in the scene; titles can.
EXCLUDE_TITLE_RE = re.compile(r"\b(cover|poster)s?\b", re.I)


def usable_pool(library, config):
    hidden = set(config["hiddenImages"])
    return [e for e in library["images"].values()
            if e.get("path") and e["artist"] in config["artists"]
            and f"{e['artist']}/{e['id']}" not in hidden
            and not EXCLUDE_TITLE_RE.search(e.get("title", "")) and Path(e["path"]).is_file()]


def theme_counts(pool):
    counts = {}
    for e in pool:
        for tag in [t for t in e.get("tags") or [] if t in TAG_GROUP]:
            counts[tag] = counts.get(tag, 0) + 1
    return counts


def distinct_projects(entries):
    return len({e["project"] for e in entries})


def pick_themes(pool, config, need, last_key, rng, anchor=None):
    """1 or 2 themes whose matching images span at least `need` projects
    and include at least one image of the `anchor` artist."""
    tagged = [e for e in pool if e.get("tags")]
    if not tagged:
        return []
    disabled = set(config["disabledThemes"])
    counts = theme_counts(tagged)

    def matching(themes):
        return [e for e in tagged if all(t in e["tags"] for t in themes)]

    def specific(t):
        return counts[t] <= GENERIC_SHARE * len(tagged)

    def fits(themes):
        hits = matching(themes)
        return distinct_projects(hits) >= need and (
            anchor is None or any(e["artist"] == anchor for e in hits))

    singles = [t for t in counts if t not in disabled and specific(t) and fits([t])]
    if not singles:  # tiny or uniform library: accept generic themes too
        singles = [t for t in counts if t not in disabled and fits([t])]
    if not singles:
        return []

    for _ in range(20):
        first = rng.choice(singles)
        themes = [first]
        if rng.random() < PAIR_CHANCE:
            partners = [t for t in counts
                        if t not in disabled and specific(t) and TAG_GROUP[t] != TAG_GROUP[first]
                        and fits([first, t])]
            if partners:
                themes.append(rng.choice(partners))
        if "+".join(sorted(themes)) != last_key:
            return themes
    return themes


def load_palettes():
    """{id: preset} from palettes.json, each with its family and a display label."""
    palettes = {}
    for family in read_json(PALETTES_PATH, {}).get("families", []):
        for preset in family["variants"]:
            label = family["name"] if preset["name"] == family["name"] \
                else f"{family['name']} {preset['name']}"
            palettes[preset["id"]] = dict(preset, family=family["name"], label=label)
    return palettes


def active_palette(config):
    """The selected palette preset, or None (none selected, or no longer in the catalog)."""
    return load_palettes().get(config.get("palette") or "")


def palette_colors(preset):
    """A palette as CIELAB colors: its background-to-foreground ramp plus its accents.
    The ramp lets mid tones of the right tint count, not only the extremes."""
    bg, fg = hex_to_lab(preset["background"]), hex_to_lab(preset["foreground"])
    ramp = [tuple(b + (f - b) * step / 4 for b, f in zip(bg, fg)) for step in range(5)]
    return ramp + [hex_to_lab(c) for c in preset.get("accents", [])]


def palette_distance(entry, colors):
    """Mean CIELAB distance of the image's dominant colors to their nearest palette color."""
    return sum(share * min(math.dist((L, a, b), c) for c in colors)
               for L, a, b, share in entry["swatches"])


def analyzed(entry):
    return entry.get("swatches") and entry.get("swatchVersion") == SWATCH_VERSION


def palette_tiers(pool, preset):
    """Candidate pools from strict to loose: [(match, entries)], ending with the whole
    pool so a palette can never block selection."""
    if preset is None:
        return [(None, pool)]
    colors = palette_colors(preset)
    ranked = sorted((palette_distance(e, colors), i) for i, e in enumerate(pool) if analyzed(e))
    close = [pool[i] for d, i in ranked[:int(len(ranked) * PALETTE_SHARE)] if d <= PALETTE_MAX_SCORE]
    relaxed = [pool[i] for _, i in ranked[:int(len(ranked) * RELAXED_SHARE)]]
    return [("close", close), ("relaxed", relaxed), ("none", pool)]


def pick_images(candidates, need, history, rng, anchor=None):
    """`need` images from distinct projects. Artists take turns (anchor first) so a
    prolific artist cannot crowd out the others; within an artist, images not shown
    recently come first."""
    recent = set(history)
    queues = {}
    for e in candidates:
        queues.setdefault(e["artist"], []).append(e)
    for artist, items in queues.items():
        rng.shuffle(items)
        items.sort(key=lambda e: f"{e['artist']}/{e['id']}" in recent)  # stable: fresh first
    order = sorted(queues, key=lambda a: (a != anchor, rng.random()))
    chosen, projects = [], set()
    while len(chosen) < need and any(queues[a] for a in order):
        for artist in order:
            # Next image of this artist from a project not used yet.
            while queues[artist]:
                e = queues[artist].pop(0)
                if e["project"] not in projects:
                    chosen.append(e)
                    projects.add(e["project"])
                    break
            if len(chosen) == need:
                break
    return chosen


# ---------- rendering ----------

def screen_size():
    try:
        monitors = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True,
                                             text=True, timeout=5).stdout)
        m = max(monitors, key=lambda m: m["width"] * m["height"])
        w, h = m["width"], m["height"]
        if m.get("transform", 0) % 2 == 1:
            w, h = h, w
        return w, h
    except Exception:
        return 2560, 1440


def crop_loss(image_aspect, slot_aspect):
    """Share of the image cut away when cover-cropping it into the slot."""
    return 1 - min(image_aspect, slot_aspect) / max(image_aspect, slot_aspect)


def slot_size(w, h, gap, axis, count):
    if axis == "cols":
        return (w - (count - 1) * gap) // count, h
    return w, (h - (count - 1) * gap) // count


def layouts_for(w, h, gap):
    """Layouts that suit this screen: per image kind, as many images as fit side by
    side (wide screens) or stacked (tall screens). Falls back to the best one."""
    options = []
    for kind, ideal in IMAGE_KINDS.items():
        screen = w / h
        axis = "cols" if screen >= ideal else "rows"
        count = max(1, round(screen / ideal if axis == "cols" else ideal / screen))
        sw, sh = slot_size(w, h, gap, axis, count)
        options.append({"key": f"{kind}-{axis}-{count}", "kind": kind, "axis": axis,
                        "count": count, "slot": sw / sh, "loss": crop_loss(ideal, sw / sh)})
    good = [o for o in options if o["loss"] <= LAYOUT_TOLERANCE]
    return good or [min(options, key=lambda o: o["loss"])]


def fits_slot(entry, slot_aspect):
    return entry.get("h") and crop_loss(entry["w"] / entry["h"], slot_aspect) <= MAX_CROP


def layout_label(key):
    kind, axis, count = key.split("-")
    return f"{count} × {kind}" + (" stacked" if axis == "rows" and int(count) > 1 else "")


def gutter_color():
    try:
        match = re.search(r'^background\s*=\s*"(#[0-9a-fA-F]{6})"', THEME_COLORS.read_text(), re.M)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "#111111"


def render(entries, gap, layout, w, h):
    n = len(entries)
    sw, sh = slot_size(w, h, gap, layout["axis"], n)
    COLLAGE_DIR.mkdir(parents=True, exist_ok=True)
    out = COLLAGE_DIR / f"collage-{int(time.time() * 1000)}.jpg"
    cmd = ["magick", "-size", f"{w}x{h}", f"xc:{gutter_color()}"]
    for i, e in enumerate(entries):
        # The last slot is aligned to the far edge so rounding never leaves a seam.
        offset = i * ((sw if layout["axis"] == "cols" else sh) + gap)
        if layout["axis"] == "cols":
            x, y = (w - sw if i == n - 1 else offset), 0
        else:
            x, y = 0, (h - sh if i == n - 1 else offset)
        cmd += ["(", e["path"], "-resize", f"{sw}x{sh}^", "-gravity", "center",
                "-extent", f"{sw}x{sh}", ")", "-gravity", "northwest",
                "-geometry", f"+{x}+{y}", "-composite"]
    cmd += ["-quality", "93", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def set_background(path):
    subprocess.run(["omarchy-theme-bg-set", str(path)], check=True, capture_output=True, timeout=30)
    # Keep the new and the previous collage (the shell may still fade from it).
    old = sorted(COLLAGE_DIR.glob("collage-*.jpg"))[:-2]
    for f in old:
        f.unlink(missing_ok=True)


def describe(entries):
    return [{"key": f"{e['artist']}/{e['id']}", "artist": e["artist"], "title": e["title"],
             "link": e["project"], "tags": e.get("tags") or []} for e in entries]


def feasible_layouts(pool, w, h, gap, variety=1):
    """Layouts that suit the screen with their fitting images, if those span at
    least `variety` collages' worth of projects."""
    layouts = []
    for layout in layouts_for(w, h, gap):
        fitting = [e for e in pool if fits_slot(e, layout["slot"])]
        if distinct_projects(fitting) >= layout["count"] * variety:
            layouts.append((layout, fitting))
    return layouts


def apply_next(config, library, state, w, h):
    """Pick a layout, themes and images for this screen, render and set them."""
    pool = usable_pool(library, config)
    gap = int(config["gap"])
    rng = random.Random()
    palette = active_palette(config)

    # Layouts that suit the screen and have enough fitting images; on a 16:9
    # screen e.g. two portraits or one landscape, chosen at random. With a
    # palette, the closest matches are used if they leave enough variety.
    for match, candidates in palette_tiers(pool, palette):
        layouts = feasible_layouts(candidates, w, h, gap,
                                   PALETTE_VARIETY if match in ("close", "relaxed") else 1)
        if layouts:
            break
    if not layouts:
        sys.exit(f"Not enough fitting images ({len(pool)}). Add artists and sync first.")
    layout, fitting = rng.choice(layouts)
    need = layout["count"]

    history = state.get("history", [])
    # Each change is anchored on a random artist, so every artist gets an
    # equal share no matter how many images they have.
    anchor = rng.choice(sorted({e["artist"] for e in fitting if e.get("tags")} or {None}))
    themes = pick_themes(fitting, config, need, state.get("comboKey"), rng, anchor)
    if not themes:  # anchor has too few images for any theme
        anchor = None
        themes = pick_themes(fitting, config, need, state.get("comboKey"), rng)
    candidates = [e for e in fitting if all(t in (e.get("tags") or []) for t in themes)]
    entries = pick_images(candidates, need, history, rng, anchor)

    remember(state, pool, entries)
    return show(config, state, entries, layout, themes,
                {"id": palette["id"], "label": palette["label"], "match": match} if palette else None,
                w, h)


def remember(state, pool, entries):
    """Add shown images to the recently-shown list that new picks avoid."""
    keep = max(len(entries), distinct_projects(pool) // 2)
    state["history"] = (state.get("history", []) + [f"{e['artist']}/{e['id']}" for e in entries])[-keep:]


def pick_replacement(pool, entries, index, layout, themes, palette, history, rng):
    """An image for tile `index`: fits the slot, shares the collage's themes, comes
    from a project not in the collage yet and fits its palette as closely as
    possible. Artists with fewer tiles in the collage go first, then images not
    shown recently. None if nothing qualifies."""
    others = [e for i, e in enumerate(entries) if i != index]
    projects = {e["project"] for e in entries}  # the replaced one too: show something new
    fitting = [e for e in pool if e["project"] not in projects and fits_slot(e, layout["slot"])
               and all(t in (e.get("tags") or []) for t in themes)]
    preset = load_palettes().get(palette["id"]) if palette else None
    tiers = palette_tiers(pool, preset)
    # Start at the collage's own match level; loosen only if that has nothing.
    start = next((i for i, (match, _) in enumerate(tiers) if match == (palette or {}).get("match")), 0)
    for _, tier in tiers[start:]:
        allowed = {id(e) for e in tier}
        candidates = [e for e in fitting if id(e) in allowed]
        if candidates:
            break
    else:
        return None
    by_artist = {}
    for e in candidates:
        by_artist.setdefault(e["artist"], []).append(e)
    tiles = {a: sum(1 for e in others if e["artist"] == a) for a in by_artist}
    fewest = min(tiles.values())
    artist = rng.choice(sorted(a for a in by_artist if tiles[a] == fewest))
    recent = set(history)
    items = by_artist[artist]
    rng.shuffle(items)
    items.sort(key=lambda e: f"{e['artist']}/{e['id']}" in recent)  # stable: fresh first
    return items[0]


def show(config, state, entries, layout, themes, palette, w, h):
    """Render and set a new selection. The one it replaces becomes `previous`,
    so Back can restore it."""
    out = render(entries, int(config["gap"]), layout, w, h)
    set_background(out)
    current = {"themes": themes, "images": describe(entries), "layout": layout["key"],
               "screen": [w, h], "path": str(out), "at": int(time.time())}
    if palette:
        current["palette"] = palette
    if state.get("current"):
        state["previous"] = state["current"]
    state.update(current=current, comboKey="+".join(sorted(themes)))
    write_json(STATE_PATH, state)
    return current


def restorable(selection, library, w, h, gap):
    """(entries, layout, None) to show a stored selection again on this screen,
    or (None, None, reason) when that is not possible."""
    if not selection or not selection.get("images"):
        return None, None, "No previous collage yet"
    entries = [library["images"].get(i["key"]) for i in selection["images"]]
    if not all(e and e.get("path") and Path(e["path"]).is_file() for e in entries):
        return None, None, "An image of that collage is no longer downloaded"
    # Selections from before layouts existed were portrait columns.
    key = selection.get("layout") or f"portrait-cols-{len(entries)}"
    layout = next((l for l in layouts_for(w, h, gap) if l["key"] == key), None)
    if layout is None or not all(fits_slot(e, layout["slot"]) for e in entries):
        return None, None, "That collage does not fit this screen"
    return entries, layout, None


def cmd_next():
    with Lock("render"):
        w, h = screen_size()
        current = apply_next(load_config(), read_json(LIBRARY_PATH, {"images": {}}),
                             read_json(STATE_PATH, {}), w, h)
        palette = current.get("palette")
        print(f"{layout_label(current['layout'])}: {' + '.join(current['themes']) or 'random'}"
              + (f" · {palette['label']} ({palette['match']} match)" if palette else ""))


def cmd_rerender(only_if_screen_changed=False):
    """Re-apply the current selection after an Omarchy theme switch (new gutter
    color) or a screen change. If the current layout no longer suits the screen,
    a fresh selection is made instead."""
    config = load_config()
    if not config["enabled"]:
        return  # called from hooks and screen changes; stay out of the way when off
    with Lock("render"):
        library = read_json(LIBRARY_PATH, {"images": {}})
        state = read_json(STATE_PATH, {})
        current = state.get("current")
        if not current:
            return
        w, h = screen_size()
        if only_if_screen_changed and current.get("screen") == [w, h]:
            return
        entries, layout, problem = restorable(current, library, w, h, int(config["gap"]))
        if problem:
            apply_next(config, library, state, w, h)
            return
        out = render(entries, int(config["gap"]), layout, w, h)
        set_background(out)
        current.update(path=str(out), layout=layout["key"], screen=[w, h])
        write_json(STATE_PATH, state)


def cmd_back():
    """Show the previous selection again, re-rendered for this screen and theme.
    Nothing changes when it cannot be restored."""
    with Lock("render"):
        config = load_config()
        library = read_json(LIBRARY_PATH, {"images": {}})
        state = read_json(STATE_PATH, {})
        previous = state.get("previous")
        w, h = screen_size()
        entries, layout, problem = restorable(previous, library, w, h, int(config["gap"]))
        if problem:
            sys.exit(problem)
        current = show(config, state, entries, layout, previous.get("themes", []),
                       previous.get("palette"), w, h)
        print(f"{layout_label(current['layout'])}: {' + '.join(current['themes']) or 'random'}")


def cmd_replace(key):
    """Swap one tile of the current collage. Nothing changes if no image qualifies."""
    with Lock("render"):
        config = load_config()
        library = read_json(LIBRARY_PATH, {"images": {}})
        state = read_json(STATE_PATH, {})
        current = state.get("current")
        if not current:
            sys.exit("No collage yet")
        keys = [i["key"] for i in current.get("images", [])]
        if key not in keys:
            sys.exit("That image is no longer in the collage")
        w, h = screen_size()
        entries, layout, problem = restorable(current, library, w, h, int(config["gap"]))
        if problem:
            sys.exit(problem)
        pool = usable_pool(library, config)
        index = keys.index(key)
        themes = current.get("themes", [])
        entry = pick_replacement(pool, entries, index, layout, themes, current.get("palette"),
                                 state.get("history", []), random.Random())
        if entry is None:
            sys.exit("No other image fits this slot" + (f" with {' + '.join(themes)}" if themes else ""))
        entries[index] = entry
        remember(state, pool, [entry])
        show(config, state, entries, layout, themes, current.get("palette"), w, h)
        print(f"Replaced {key} with {entry['artist']}/{entry['id']}")


def cmd_status():
    config = load_config()
    library = read_json(LIBRARY_PATH, {"images": {}})
    pool = usable_pool(library, config)
    tagged = [e for e in pool if e.get("tags")]
    artists = []
    for artist in config["artists"]:
        mine = [e for e in pool if e["artist"] == artist]
        artists.append({"name": artist, "images": len(mine),
                        "projects": distinct_projects(mine) if mine else 0})
    counts = theme_counts(tagged)
    themes = [{"name": t, "group": TAG_GROUP[t], "count": c}
              for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    palettes = [{"id": p["id"], "label": p["label"], "family": p["family"],
                 "light": hex_to_lab(p["background"])[0] > 50}
                for p in load_palettes().values()]
    palette = active_palette(config)
    if palette:
        close = palette_tiers(pool, palette)[0][1]
        palette = {"id": palette["id"], "label": palette["label"], "matches": len(close),
                   "matchProjects": distinct_projects(close) if close else 0}
    pending_colors = sum(1 for e in pool if not analyzed(e))
    hidden = []
    for key in config["hiddenImages"]:
        e = library["images"].get(key) or {}
        hidden.append({"key": key, "artist": key.split("/")[0], "title": e.get("title") or key,
                       "link": e.get("project", "")})
    sync = read_json(SYNC_PATH, {})
    if sync.get("running") and running_sync_pid() is None:
        sync["running"] = False  # the sync died without cleaning up
    state = read_json(STATE_PATH, {})
    w, h = screen_size()
    _, _, problem = restorable(state.get("previous"), library, w, h, int(config["gap"]))
    back = {"available": problem is None, "exists": bool(state.get("previous")),
            "reason": problem or ""}
    current = state.get("current") or {}
    if current.get("images"):
        current["layoutLabel"] = layout_label(
            current.get("layout") or f"portrait-cols-{len(current['images'])}")
    print(json.dumps({"artists": artists, "images": len(pool), "tagged": len(tagged),
                      "themes": themes, "palettes": palettes, "palette": palette,
                      "pendingColors": pending_colors, "back": back, "hidden": hidden,
                      "sync": sync,
                      "state": state}, ensure_ascii=False))


COMMANDS = {"init": cmd_init, "migrate": cmd_migrate,
            "sync": cmd_sync, "cancel": cmd_cancel, "next": cmd_next, "rerender": cmd_rerender,
            "back": cmd_back, "status": cmd_status}

if __name__ == "__main__":
    args = sys.argv[1:]
    if args != ["migrate"]:
        try:
            prepare_data()
        except (OSError, RuntimeError) as e:
            sys.exit(str(e))
    if args == ["rerender", "--if-changed"]:
        cmd_rerender(only_if_screen_changed=True)
    elif len(args) == 2 and args[0] == "replace":
        cmd_replace(args[1])
    elif len(args) == 1 and args[0] in COMMANDS:
        try:
            COMMANDS[args[0]]()
        except (OSError, ValueError, RuntimeError) as e:
            sys.exit(str(e))
    else:
        sys.exit(f"usage: scenula.py {{{'|'.join(COMMANDS)}}} | rerender --if-changed"
                 " | replace <artist/id>")
