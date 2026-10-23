#!/usr/bin/env python3
"""Tessella backend: sync artists, tag images, render triptychs.

Commands:
  sync      fetch every configured artist's RSS feed, download new images,
            drop removed artists, tag untagged images via Ollama
  cancel    stop a running sync; what is done so far is kept
  next      pick a layout for the screen (e.g. 3 portraits on 21:9, 2 portraits
            or 1 landscape on 16:9), 1-2 themes and images; render and set it
  rerender  render the current selection again (new theme colors); picks a
            fresh one if the layout no longer suits the screen
            --if-changed: only act when the screen size differs
  status    print a JSON summary for the shell panel

Files (single writer each):
  config   ~/.config/omarchy/tessella/config.json   written by the panel
  library  ~/.local/share/tessella/library.json          written by sync
  sync     ~/.local/state/tessella/sync.json             written by sync
  state    ~/.local/state/tessella/state.json            written by next/rerender
"""

import base64
import concurrent.futures
import fcntl
import html
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = HOME / ".config/omarchy/tessella/config.json"
DATA_DIR = HOME / ".local/share/tessella"
STATE_DIR = HOME / ".local/state/tessella"
LIBRARY_PATH = DATA_DIR / "library.json"
IMAGES_DIR = DATA_DIR / "images"
COLLAGE_DIR = DATA_DIR / "collages"
SYNC_PATH = STATE_DIR / "sync.json"
STATE_PATH = STATE_DIR / "state.json"
THEME_COLORS = HOME / ".local/state/omarchy/current/theme/colors.toml"

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


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_config():
    config = dict(DEFAULT_CONFIG)
    config.update(read_json(CONFIG_PATH, {}))
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
    if len(args) >= 2 and args[-1] == b"sync" and args[-2].endswith(b"tessella.py"):
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
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    try:
        futures = {pool.submit(download_entry, e): e for e in pending}
        for n, future in enumerate(concurrent.futures.as_completed(futures), 1):
            entry = futures[future]
            try:
                future.result()
                entry.pop("new", None)
            except Exception as e:  # keep going; the image is retried next sync
                if entry.get("new"):
                    del images[f"{entry['artist']}/{entry['id']}"]
                progress.error(f"Download {entry['url']}: {e}")
            if n % 10 == 0 or n == len(pending):
                progress.set(done=n)
                write_json(LIBRARY_PATH, library)
    finally:
        # On cancel, drop queued downloads instead of waiting for all of them.
        pool.shutdown(wait=False, cancel_futures=True)
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
    return [e for e in library["images"].values()
            if e.get("path") and e["artist"] in config["artists"]
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


def apply_next(config, library, state, w, h):
    """Pick a layout, themes and images for this screen, render and set them."""
    pool = usable_pool(library, config)
    gap = int(config["gap"])
    rng = random.Random()

    # Layouts that suit the screen and have enough fitting images; on a 16:9
    # screen e.g. two portraits or one landscape, chosen at random.
    layouts = []
    for layout in layouts_for(w, h, gap):
        fitting = [e for e in pool if fits_slot(e, layout["slot"])]
        if distinct_projects(fitting) >= layout["count"]:
            layouts.append((layout, fitting))
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

    out = render(entries, gap, layout, w, h)
    set_background(out)

    keep = max(need, distinct_projects(pool) // 2)
    history = (history + [f"{e['artist']}/{e['id']}" for e in entries])[-keep:]
    write_json(STATE_PATH, {"current": {"themes": themes, "images": describe(entries),
                                        "layout": layout["key"], "screen": [w, h],
                                        "path": str(out), "at": int(time.time())},
                            "comboKey": "+".join(sorted(themes)), "history": history})
    return themes, layout


def cmd_next():
    with Lock("render"):
        w, h = screen_size()
        themes, layout = apply_next(load_config(), read_json(LIBRARY_PATH, {"images": {}}),
                                    read_json(STATE_PATH, {}), w, h)
        print(f"{layout_label(layout['key'])}: {' + '.join(themes) or 'random'}")


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
        entries = [library["images"].get(i["key"]) for i in current.get("images", [])]
        # Selections from before layouts existed were portrait columns.
        key = current.get("layout") or f"portrait-cols-{len(entries)}"
        layout = next((l for l in layouts_for(w, h, int(config["gap"])) if l["key"] == key), None)
        usable = all(e and e.get("path") and Path(e["path"]).is_file() for e in entries)
        if layout is None or not usable or not all(fits_slot(e, layout["slot"]) for e in entries):
            apply_next(config, library, state, w, h)
            return
        out = render(entries, int(config["gap"]), layout, w, h)
        set_background(out)
        current.update(path=str(out), layout=key, screen=[w, h])
        write_json(STATE_PATH, state)


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
    sync = read_json(SYNC_PATH, {})
    if sync.get("running") and running_sync_pid() is None:
        sync["running"] = False  # the sync died without cleaning up
    state = read_json(STATE_PATH, {})
    current = state.get("current") or {}
    if current.get("images"):
        current["layoutLabel"] = layout_label(
            current.get("layout") or f"portrait-cols-{len(current['images'])}")
    print(json.dumps({"artists": artists, "images": len(pool), "tagged": len(tagged),
                      "themes": themes, "sync": sync,
                      "state": state}, ensure_ascii=False))


COMMANDS = {"sync": cmd_sync, "cancel": cmd_cancel, "next": cmd_next, "rerender": cmd_rerender,
            "status": cmd_status}

if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["rerender", "--if-changed"]:
        cmd_rerender(only_if_screen_changed=True)
    elif len(args) == 1 and args[0] in COMMANDS:
        COMMANDS[args[0]]()
    else:
        sys.exit(f"usage: tessella.py {{{'|'.join(COMMANDS)}}} | rerender --if-changed")
