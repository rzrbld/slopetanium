# SPDX-License-Identifier: MIT
"""Slopetanium core: watched GitHub repos, release checks, downloads, and
installs through PortMaster's own harbourmaster.

Python 3.7 compatible (some CFWs still ship it): no walrus, no PEP 585/604.
"""

import fnmatch
import hashlib
import json
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback

try:
    import requests  # PortMaster vendors it in exlibs/ (with certifi)
except Exception:  # pragma: no cover - fallback for bare systems
    requests = None

import urllib.request
import urllib.error

import gog as gog_mod
import community as community_mod

USER_AGENT = "slopetanium/0.1"
API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# The ports Slopetanium knows from the start (rzrbld's recompiled ports, and itself). Added to the watch list once
# (settings["catalogue_offered"] remembers which were offered, so a removed one stays removed).
#   originals_dir / originals_pattern: where the user's own game files go ("." = the port folder itself)
#   aliases: other names the port's port.json may carry (a zip installed by hand)
#   gog: the game on GOG.com: its product id, where its installer goes, and the installer's file names
#   cover_url: the game's box art (GOG's and Steam's store art, TheGamesDB's scans): fetched once into conf/art,
#              never shipped
CATALOGUE = [
    {"repo": "rzrbld/ReNFS5", "cover_url": "https://cdn.thegamesdb.net/images/original/boxart/front/10855-1.jpg", "hint": "Your installed game folder (Porsche.exe, Drivers/dx7z.dll, FEData/, GameData/), or a ZIP of it.", "title": "ReNFS5 (Need for Speed: Porsche Unleashed)", "originals_dir": "source"},
    {"repo": "rzrbld/The-ReThing", "cover_url": "https://cdn.thegamesdb.net/images/original/boxart/front/10842-1.jpg", "hint": "Your game ISO (it is saved as the_thing.iso).", "rename": {"*.iso": "the_thing.iso"}, "title": "The ReThing (The Thing)", "originals_dir": "source"},
    {"repo": "rzrbld/ReConflict", "cover_url": "https://images.gog-statics.com/8fbb8a146ec9f9400cf5bf5f21e0880b7523b86d99d79d8c0016a6df43c3553f_glx_vertical_cover.jpg", "hint": "The GOG installer, setup_conflict_-_desert_storm_1.0_(21657).exe, with its .bin parts if it has any.", "buy": [{"store": "GOG.com", "url": "https://www.gog.com/en/game/conflict_desert_storm"}], "title": "ReConflict (Conflict: Desert Storm)", "originals_dir": ".", "originals_pattern": "setup_*.exe,setup_*.bin",
     "gog": {"id": 1207658998, "title": "Conflict: Desert Storm", "dest": ".", "files": "setup_conflict*"}},
    {"repo": "rzrbld/vurus", "cover_url": "https://raw.githubusercontent.com/rzrbld/vurus/main/docs/images/port-logo.png", "hint": "Your original disc image: an ISO, or a single-BIN CUE/BIN.", "title": "Vurus (Vyruz)", "originals_dir": ".", "originals_pattern": "*.iso,*.cue,*.bin", "aliases": ["vurus.zip"]},
    {"repo": "rzrbld/TheFamilia", "cover_url": "https://cdn.cloudflare.steamstatic.com/steam/apps/40990/library_600x900.jpg", "hint": "The three Mafia CD images (.bin+.cue or .iso), mafia_patch_1.2_eng.exe and an unprotected 1.2 Game.exe (loose or in a .zip/.rar/.7z).", "title": "The Familia (Mafia)", "originals_dir": "source"},
    {"repo": "rzrbld/ReRED_Faction_Duo", "cover_url": "https://images.gog-statics.com/5cca51d2be9cdb56229c5a1bcd6b400cfeba6486351dc1227a61869149af03e5.jpg", "hint": "The GOG installer, setup_red_faction2_2.0.0.6.exe.", "buy": [{"store": "GOG.com", "url": "https://www.gog.com/en/game/red_faction_2"}], "title": "ReRed Faction II", "originals_dir": "source",
     "gog": {"id": 1207660613, "title": "Red Faction 2", "dest": "source", "files": "setup_red_faction2_*"}},
    {"repo": "rzrbld/slopetanium", "title": "Slopetanium", "originals_dir": ""},
]

# Files in a port's originals folder that are part of the port, not the user's.
KEEP_ORIGINALS = ("put_your_*", "readme*", ".keep", ".gitkeep")


def now():
    return int(time.time())


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def parse_repo(text):
    """Accept 'owner/name', a github.com URL, or a releases URL."""
    text = (text or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", text)
    if m:
        text = "%s/%s" % (m.group(1), m.group(2))
    if text.endswith(".git"):
        text = text[:-4]
    text = text.strip("/")
    if not REPO_RE.match(text):
        raise ValueError("Expected owner/name or a github.com link, got: %s" % text)
    return text


def name_cleaner(text):
    """harbourmaster's port name for a zip file name (util/text.py)."""
    temp = re.sub(r"[^a-zA-Z0-9 _\-\.]+", "", text.strip().lower())
    return re.sub(r"[ \.]+", ".", temp)


def file_hash(path, algo="md5"):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def battery():
    """(percent or None, charging)"""
    base = "/sys/class/power_supply"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None, False
    for n in names:
        p = os.path.join(base, n)
        try:
            with open(os.path.join(p, "type")) as fh:
                if fh.read().strip() != "Battery":
                    continue
            with open(os.path.join(p, "capacity")) as fh:
                cap = int(fh.read().strip())
            st = ""
            try:
                with open(os.path.join(p, "status")) as fh:
                    st = fh.read().strip()
            except (IOError, OSError):
                pass
            return cap, st in ("Charging", "Full")
        except (IOError, OSError, ValueError):
            continue
    return None, False


def free_space(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


# the most one file can be, by filesystem (the rest: as big as the card)
FS_MAX_FILE = {"vfat": 4 * 1024 ** 3 - 1, "msdos": 4 * 1024 ** 3 - 1, "fat": 4 * 1024 ** 3 - 1, "umsdos": 4 * 1024 ** 3 - 1}
_fs_cache = {}


def fs_info(path):
    """{"fs": the filesystem holding path, "max_file": its largest file in bytes, or None for no practical limit}.
    From /proc/mounts: the longest mount point the path is under (a card can be FAT32 on one handheld, exFAT or ext4
    on another)."""
    try:
        real = os.path.realpath(path)
    except OSError:
        real = path
    hit = _fs_cache.get(real)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    best, fs = "", None
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                    best, fs = mp, parts[2]
    except (IOError, OSError):
        pass
    if fs == "fuseblk" or (fs or "").startswith("fuse"):
        fs = fs  # (exFAT/NTFS through FUSE: no 4 GB limit)
    info = {"fs": {"vfat": "FAT32", "msdos": "FAT", "fat": "FAT"}.get(fs, fs), "max_file": FS_MAX_FILE.get(fs)}
    _fs_cache[real] = (time.time(), info)
    return info


def check_fits(path, size, what="This file"):
    lim = fs_info(path)
    if lim["max_file"] and size > lim["max_file"]:
        raise ValueError("%s is %s: over %s, the most one file can be on this card (%s). Format the card as exFAT, "
                         "or use a smaller copy." % (what, human_size(size), human_size(lim["max_file"]), lim["fs"]))


BACKUP_MANIFEST = "slopetanium-backup.json"
LOW_BATTERY = 20


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr"], stderr=subprocess.DEVNULL,
                                      timeout=2).decode("utf-8", "replace")
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", out):
            ip = m.group(1)
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    ips = [ip for ip in ips if not ip.startswith("127.")]

    def rank(ip):  # home Wi-Fi first, container bridges last
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if re.match(r"172\.(1[7-9]|2\d|3[01])\.0\.1$", ip):
            return 3
        return 2
    return sorted(ips, key=rank)


class HttpError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


class Http(object):
    """requests when PortMaster's exlibs are there, urllib otherwise."""

    def __init__(self, token_getter):
        self.token_getter = token_getter

    def _headers(self, extra=None, api=True):
        h = {"User-Agent": USER_AGENT}
        if api:
            h["Accept"] = "application/vnd.github+json"
            h["X-GitHub-Api-Version"] = "2022-11-28"
            tok = self.token_getter()
            if tok:
                h["Authorization"] = "Bearer " + tok
        if extra:
            h.update(extra)
        return h

    def get_json(self, url, etag=None):
        """Returns (status, data, etag, rate_remaining). 304 -> data None."""
        extra = {"If-None-Match": etag} if etag else None
        if requests is not None:
            r = requests.get(url, headers=self._headers(extra), timeout=20)
            status, body, hdrs = r.status_code, r.content, r.headers
        else:
            req = urllib.request.Request(url, headers=self._headers(extra))
            try:
                resp = urllib.request.urlopen(req, timeout=20)
                status, body, hdrs = resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as e:
                status, body, hdrs = e.code, e.read(), e.headers
        remaining = hdrs.get("X-RateLimit-Remaining")
        if status == 304:
            return status, None, etag, remaining
        if status >= 400:
            msg = "HTTP %d" % status
            try:
                msg += ": " + json.loads(body.decode("utf-8")).get("message", "")
            except Exception:
                pass
            if status in (403, 429) and remaining == "0":
                msg = "GitHub rate limit reached; add a token in Settings or wait an hour"
            raise HttpError(status, msg)
        return status, json.loads(body.decode("utf-8")), hdrs.get("ETag"), remaining

    def download(self, url, dest, progress=None, cancel=None, attempts=4):
        """Download with retries: GitHub's CDN answers 5xx now and then."""
        delay = 2
        for i in range(attempts):
            try:
                return self._download(url, dest, progress, cancel)
            except HttpError as e:
                if e.status < 500 and e.status != 429 or i == attempts - 1:
                    raise
                err = e
            except (IOError, OSError) as e:  # connection reset, timeouts
                if i == attempts - 1:
                    raise
                err = e
            if progress:
                progress(0, 0, "retry %d/%d after: %s" % (i + 2, attempts, err))
            time.sleep(delay)
            delay *= 2

    def _download(self, url, dest, progress=None, cancel=None):
        tmp = dest + ".part"
        done = 0
        if requests is not None:
            with requests.get(url, headers=self._headers(api=False), stream=True, timeout=30) as r:
                if r.status_code >= 400:
                    raise HttpError(r.status_code, "Download failed: HTTP %d" % r.status_code)
                total = int(r.headers.get("Content-Length") or 0)
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        if cancel and cancel():
                            raise RuntimeError("cancelled")
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        else:
            req = urllib.request.Request(url, headers=self._headers(api=False))
            resp = urllib.request.urlopen(req, timeout=30)
            total = int(resp.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    if cancel and cancel():
                        raise RuntimeError("cancelled")
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        os.replace(tmp, dest)
        return done


class FsConflict(Exception):
    pass


TEXT_MAX = 1 << 20   # the most the browser's editor opens
TEXT_EXT = set(".txt .ini .cfg .conf .json .xml .sh .lua .yaml .yml .toml .md .log .gptk .py .properties .csv .bat "
               ".cmd .js .htm .html .css .list .rc .def .mod .script .info .nfo".split())


def _looks_text(path, size):
    if size > TEXT_MAX:
        return False
    if os.path.splitext(path)[1].lower() in TEXT_EXT:
        return True
    try:
        with open(path, "rb") as fh:
            head = fh.read(2048)
    except (IOError, OSError):
        return False
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start > len(head) - 4  # cut inside a character at the end


def _plain_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ValueError("Bad name: %s" % (name or "(empty)"))
    return name


class InstallBlocked(ValueError):
    """Battery or space says not now; the user may insist (force)."""

    def __init__(self, problems):
        ValueError.__init__(self, "; ".join(problems))
        self.problems = problems


class UploadOffset(Exception):
    """The upload's chunk doesn't continue the file (the browser resends from have)."""

    def __init__(self, have):
        Exception.__init__(self, "resume at %d" % have)
        self.have = have


class Job(object):
    def __init__(self, kind, repo_id, label, fn):
        self.id = "%x%04x" % (now(), random.randint(0, 0xFFFF))
        self.kind = kind
        self.repo_id = repo_id
        self.label = label
        self.fn = fn
        self.state = "queued"  # queued, running, done, failed
        self.progress = None  # 0..1 or None
        self.message = ""
        self.started = None
        self.finished = None

    def to_dict(self):
        return {
            "id": self.id, "kind": self.kind, "repo_id": self.repo_id, "label": self.label,
            "state": self.state, "progress": self.progress, "message": self.message,
            "started": self.started, "finished": self.finished,
        }


class Core(object):
    def __init__(self, root, controlfolder, ports_dir=None, python=None, harbourmaster=None, catalogue=True):
        self.root = os.path.abspath(root)
        self.controlfolder = controlfolder
        self.ports_dir = os.path.abspath(ports_dir or os.path.dirname(self.root))
        self.python = python or sys.executable or "python3"
        self.harbourmaster = harbourmaster or (
            os.path.join(controlfolder, "harbourmaster") if controlfolder else None)
        self.conf_dir = os.path.join(self.root, "conf")
        self.cache_dir = os.path.join(self.conf_dir, "cache")
        self.backup_dir = os.path.join(self.conf_dir, "backups")
        self.state_file = os.path.join(self.conf_dir, "state.json")
        for d in (self.conf_dir, self.cache_dir, self.backup_dir):
            if not os.path.isdir(d):
                os.makedirs(d)

        self.lock = threading.RLock()
        self.log_lines = []
        self.jobs = []
        self.job_queue = queue.Queue()
        self.rate_remaining = None
        self.changed = 0  # bumped on every state change (screen redraw / web poll)
        self.state = self._load()
        self.save()  # persists a new PIN at once
        self.http = Http(lambda: self.state["settings"].get("github_token", ""))
        self.gog = gog_mod.Gog(self.state["settings"].setdefault("gog", {}), self.save, self.log)
        self.gog.fits = lambda d, size, name: check_fits(d, size, name)
        self.community = community_mod.Community(self.state["settings"], self.save, self.log)
        if catalogue:
            self._seed_catalogue()
        self._stop = threading.Event()
        threading.Thread(target=self._worker, name="jobs", daemon=True).start()
        threading.Thread(target=self._auto_checker, name="autocheck", daemon=True).start()
        threading.Thread(target=self._gog_refresher, name="gog", daemon=True).start()
        threading.Thread(target=self._art_fetcher, name="art", daemon=True).start()
        t = threading.Timer(15, self.sync_installs)
        t.daemon = True
        t.start()

    # ---------------------------------------------------------------- state
    def _load(self):
        state = {}
        if os.path.isfile(self.state_file):
            try:
                with open(self.state_file, "r") as fh:
                    state = json.load(fh)
            except Exception as e:
                self.log("state.json unreadable (%s), starting fresh" % e)
                try:
                    shutil.copy(self.state_file, self.state_file + ".bad")
                except OSError:
                    pass
        state.setdefault("version", 1)
        state.setdefault("repos", [])
        s = state.setdefault("settings", {})
        s.setdefault("github_token", "")
        s.setdefault("auto_check_minutes", 360)
        s.setdefault("keep_versions", 2)
        s.setdefault("web_port", 8686)
        s.setdefault("pin", "%04d" % random.randint(0, 9999))
        s.setdefault("gog_auto", True)  # fetch a port's game files from GOG after installing it
        s.setdefault("catalogue_offered", [])
        s.setdefault("count_installs", None)  # community install counts: None until the owner answered
        for r in state["repos"]:
            self._repo_defaults(r)
        return state

    def _repo_defaults(self, r):
        r.setdefault("asset_pattern", "")
        r.setdefault("prereleases", True)
        r.setdefault("port_name", "")  # harbourmaster's name: the zip file name, lower case
        r.setdefault("installed_tag", None)
        r.setdefault("shared", False)  # shared with the community (slopetanium.pw)
        r.setdefault("installed_md5", None)
        r.setdefault("installed_at", None)
        r.setdefault("history", [])  # [{tag, at}] newest first
        r.setdefault("releases", [])
        r.setdefault("etag", None)
        r.setdefault("last_checked", None)
        r.setdefault("error", None)
        r.setdefault("originals_dir", "source")
        r.setdefault("keep_on_uninstall", ["conf"])
        r.setdefault("seen_tag", None)  # newest tag the user has seen (for "new" badge)
        r.setdefault("originals_pattern", "")
        r.setdefault("aliases", [])
        r.setdefault("gog", None)  # {"id", "title", "dest", "files"}
        r.setdefault("hint", "")    # what the port wants as game files (shown with the upload)
        r.setdefault("buy", [])     # [{"store", "url"}]: where the game is sold, in the version the port supports
        r.setdefault("rename", {})  # {glob: name}: an upload's name the port needs (one file)
        r.setdefault("cover_url", "")
        r.setdefault("skipped", [])  # releases the user marked unstable: never offered as the update

    def save(self):
        with self.lock:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.state, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.state_file)
            self.changed += 1

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        print("[slopetanium] " + msg, flush=True)
        with self.lock:
            self.log_lines.append(line)
            del self.log_lines[:-300]
            self.changed += 1

    def settings(self):
        return self.state["settings"]

    def update_settings(self, data):
        with self.lock:
            s = self.state["settings"]
            if "github_token" in data:
                s["github_token"] = str(data["github_token"] or "").strip()
            if "auto_check_minutes" in data:
                s["auto_check_minutes"] = max(0, int(data["auto_check_minutes"]))
            if "keep_versions" in data:
                s["keep_versions"] = max(1, min(20, int(data["keep_versions"])))
            if "gog_auto" in data:
                s["gog_auto"] = bool(data["gog_auto"])
            if "count_installs" in data:
                was = s.get("count_installs")
                s["count_installs"] = None if data["count_installs"] is None else bool(data["count_installs"])
                if s["count_installs"] and not was:
                    threading.Thread(target=self.sync_installs, daemon=True).start()
            if "community_url" in data:
                u = str(data["community_url"] or "").strip().rstrip("/")
                if u and not (u.startswith("https://") or u.startswith("http://127.0.0.1") or u.startswith("http://localhost")):
                    raise ValueError("The community server is an https:// address")
                s["community_url"] = u
            if data.get("new_pin"):
                s["pin"] = "%04d" % random.randint(0, 9999)
            self.save()

    # ---------------------------------------------------------------- repos
    def get_repo(self, repo_id):
        for r in self.state["repos"]:
            if r["id"] == repo_id:
                return r
        raise KeyError("No such repo: %s" % repo_id)

    def add_repo(self, data):
        repo = parse_repo(data.get("repo"))
        with self.lock:
            for r in self.state["repos"]:
                if r["repo"].lower() == repo.lower() and \
                        r.get("asset_pattern", "") == (data.get("asset_pattern") or "").strip():
                    raise ValueError("%s is already watched" % repo)
            r = {"id": "%x%03x" % (now(), random.randint(0, 0xFFF)), "repo": repo, "added": now()}
            self._repo_defaults(r)
            self._apply_repo_fields(r, data)
            self.state["repos"].append(r)
            self.save()
        self.log("watching %s" % repo)
        self.queue_check(r["id"])
        if data.get("share"):
            try:
                self.share_repo(r["id"], True)
            except Exception as e:
                r["share_error"] = str(e)
        return r

    # ---------------------------------------------------------------- the community
    def share_repo(self, repo_id, on):
        r = self.get_repo(repo_id)
        if on:
            res = self.community.share(r)
            if res.get("repo") and res["repo"].lower() == r["repo"].lower():
                r["repo"] = res["repo"]  # GitHub's own spelling
            self.log("%s: shared with the community" % r["repo"])
        else:
            try:
                res = self.community.unshare(r["repo"])
            except community_mod.CommunityError as e:
                if e.status != 404:
                    raise
                res = {"shared": False}
            self.log("%s: no longer shared" % r["repo"])
        with self.lock:
            r["shared"] = bool(on)
            self.save()
        self.changed += 1
        if on:
            getattr(self, "_art_tried", {}).pop(self._cover_path(r), None)  # its cover may be there now
            if r.get("installed_tag") and self.port_dir(r):
                self.community.count_shared(r["repo"], r["installed_tag"])
            self.sync_installs()
        return res

    def sync_installs(self):
        """Installed versions to the community's counts (when the owner agreed)."""
        try:
            installed = self.installed_ports()
            pairs = [(r["repo"], r.get("installed_tag")) for r in self.state["repos"]
                     if r.get("installed_tag") and self._installed_info(r, installed)]
        except Exception:
            return
        self.community.sync_installs(pairs)

    def watched_by_repo(self):
        return dict((r["repo"].lower(), r) for r in self.state["repos"])

    def community_ports(self, q="", sort="popular"):
        res = self.community.ports(q, sort)
        mine = self.watched_by_repo()
        for p in res.get("ports", []):
            w = mine.get(p["repo"].lower())
            p["watched"] = w["id"] if w else None
        return res

    def community_port(self, repo):
        d = self.community.port(repo)
        w = self.watched_by_repo().get(d["repo"].lower())
        d["watched"] = w["id"] if w else None
        d["installed_tag"] = w.get("installed_tag") if w else None
        d["shared_here"] = bool(w and w.get("shared"))
        return d

    def community_review(self, repo, rating, text):
        w = self.watched_by_repo().get(repo.lower())
        res = self.community.review(repo, rating, text, (w or {}).get("installed_tag") or "")
        self.log("%s: review posted (%d stars)" % (repo, int(rating)))
        return res

    def add_from_community(self, repo):
        """Watch a community port, with what its sharer said it needs."""
        d = self.community.port(repo)
        if d["repo"].lower() in self.watched_by_repo():
            raise ValueError("%s is already watched" % d["repo"])
        data = dict((k, v) for k, v in (d.get("config") or {}).items() if k in community_mod.SHARED_FIELDS)
        data.update({"repo": d["repo"], "title": d.get("title") or "", "cover_url": d.get("cover_url") or ""})
        return self.add_repo(data)

    def _apply_repo_fields(self, r, data):
        if "asset_pattern" in data:
            r["asset_pattern"] = (data.get("asset_pattern") or "").strip()
        if "prereleases" in data:
            r["prereleases"] = bool(data["prereleases"])
        if "originals_dir" in data:
            od = (data.get("originals_dir") or "").strip().strip("/")
            if od and (".." in od.split("/") or od.startswith("/")):
                raise ValueError("Originals folder must be inside the port folder")
            r["originals_dir"] = od
        if "keep_on_uninstall" in data:
            keep = data["keep_on_uninstall"]
            if isinstance(keep, str):
                keep = [k.strip() for k in keep.split(",")]
            r["keep_on_uninstall"] = [k.strip("/") for k in keep if k and ".." not in k]
        if "port_name" in data and data["port_name"]:
            r["port_name"] = name_cleaner(str(data["port_name"]))
        if "title" in data:
            r["title"] = str(data.get("title") or "").strip()
        if "cover_url" in data:
            u = str(data.get("cover_url") or "").strip()
            if u and not u.startswith("https://"):
                raise ValueError("The cover must be an https:// address")
            if u != r.get("cover_url"):
                r["cover_url"] = u
                p = self._cover_path(r)
                if p and os.path.isfile(p):
                    os.unlink(p)  # fetched again
        if "hint" in data:
            r["hint"] = str(data.get("hint") or "").strip()
        if "buy" in data and isinstance(data["buy"], list):
            r["buy"] = [{"store": str(b.get("store") or ""), "url": str(b.get("url") or "")} for b in data["buy"]
                        if isinstance(b, dict) and str(b.get("url") or "").startswith("https://")]
        if "rename" in data and isinstance(data["rename"], dict):
            r["rename"] = dict((str(k), os.path.basename(str(v))) for k, v in data["rename"].items() if v)
        if "originals_pattern" in data:
            r["originals_pattern"] = (data.get("originals_pattern") or "").strip()
        if "aliases" in data:
            al = data["aliases"]
            if isinstance(al, str):
                al = al.split(",")
            r["aliases"] = [name_cleaner(a) for a in al if a and a.strip()]
        if "gog" in data:
            g = data["gog"]
            if not g or not str((g or {}).get("id") or "").strip():
                r["gog"] = None
            else:
                dest = (str(g.get("dest") or "source")).strip().strip("/") or "."
                if ".." in dest.split("/") or dest.startswith("/"):
                    raise ValueError("The GOG files' folder must be inside the port folder")
                r["gog"] = {"id": int(g["id"]), "title": str(g.get("title") or "").strip(), "dest": dest,
                            "files": str(g.get("files") or "").strip()}

    def _seed_catalogue(self):
        """The catalogue's ports on the watch list, each offered once; what the catalogue knows about a watched one
        (hints, store links, renames...) filled in where the entry has nothing of its own."""
        added = []
        with self.lock:
            for c in CATALOGUE:
                for r in self.state["repos"]:
                    if r["repo"].lower() == c["repo"].lower():
                        for k in ("title", "hint", "buy", "rename", "aliases", "gog", "originals_pattern", "cover_url"):
                            if c.get(k) and not r.get(k):
                                r[k] = c[k]
            offered = self.state["settings"].setdefault("catalogue_offered", [])
            watched = set(r["repo"].lower() for r in self.state["repos"])
            for c in CATALOGUE:
                key = c["repo"].lower()
                if key in offered:
                    continue
                offered.append(key)
                if key in watched:
                    # already watched: give it what the catalogue knows (where its files go, GOG)
                    for r in self.state["repos"]:
                        if r["repo"].lower() == key:
                            for k in ("title", "originals_pattern", "aliases", "gog"):
                                if c.get(k) and not r.get(k):
                                    r[k] = c[k]
                            if "originals_dir" in c and r.get("originals_dir") in (None, "", "source"):
                                r["originals_dir"] = c["originals_dir"]
                    continue
                r = {"id": "%x%03x" % (now(), random.randint(0, 0xFFF)), "repo": c["repo"], "added": now()}
                self._repo_defaults(r)
                for k in ("title", "originals_dir", "originals_pattern", "aliases", "gog", "hint", "buy", "rename", "cover_url"):
                    if k in c:
                        r[k] = c[k]
                self.state["repos"].append(r)
                added.append(c["repo"])
            self.save()
        for a in added:
            self.log("watching %s (catalogue)" % a)

    def catalogue(self):
        watched = dict((r["repo"].lower(), r["id"]) for r in self.state["repos"])
        return [{"repo": c["repo"], "watched": c["repo"].lower() in watched, "gog": bool(c.get("gog")),
                 "id": watched.get(c["repo"].lower()),
                 "title": c.get("title") or c["repo"].split("/")[1], "cover_url": c.get("cover_url") or "",
                 "hint": c.get("hint") or ""} for c in CATALOGUE]

    def add_from_catalogue(self, repo):
        for c in CATALOGUE:
            if c["repo"].lower() == repo.lower():
                data = {"repo": c["repo"]}
                for k in ("title", "originals_dir", "originals_pattern", "aliases", "gog", "hint", "buy", "rename", "cover_url"):
                    if k in c:
                        data[k] = c[k]
                return self.add_repo(data)
        raise ValueError("%s is not in the catalogue" % repo)

    def update_repo(self, repo_id, data):
        with self.lock:
            r = self.get_repo(repo_id)
            if data.get("repo"):
                new = parse_repo(data["repo"])
                if new.lower() != r["repo"].lower():
                    r["repo"], r["etag"], r["releases"] = new, None, []
            self._apply_repo_fields(r, data)
            r["etag"] = None  # filters changed: re-read releases
            self.save()
        self.queue_check(repo_id)
        return r

    def delete_repo(self, repo_id, drop_cache=True):
        with self.lock:
            r = self.get_repo(repo_id)
            self.state["repos"].remove(r)
            self.save()
        if drop_cache:
            shutil.rmtree(self._cache_dir(r), ignore_errors=True)
        self.log("stopped watching %s (installed files untouched)" % r["repo"])

    def mark_seen(self, repo_id=None):
        with self.lock:
            for r in self.state["repos"]:
                if repo_id is None or r["id"] == repo_id:
                    latest = self.latest_release(r)
                    if latest:
                        r["seen_tag"] = latest["tag"]
            self.save()

    # ---------------------------------------------------------------- releases
    def _pick_asset(self, r, assets):
        zips = [a for a in assets if a["name"].lower().endswith(".zip")]
        pats = [p.strip() for p in (r.get("asset_pattern") or "").split(",") if p.strip()]
        if pats:
            cand = [a for a in assets if any(fnmatch.fnmatch(a["name"].lower(), p.lower()) for p in pats)]
        else:
            cand = zips
        if len(cand) > 1 and r.get("port_name"):
            same = [a for a in cand if name_cleaner(a["name"]) == r["port_name"]]
            if same:
                cand = same
        if len(cand) > 1 and not pats:
            # Prefer something that is not a source/debug bundle.
            plain = [a for a in cand if not re.search(r"(source|src|debug|symbols)", a["name"], re.I)]
            cand = plain or cand
        return cand[0] if cand else None

    def releases(self, r):
        out = []
        for rel in r.get("releases", []):
            if rel.get("prerelease") and not r.get("prereleases", True):
                continue
            out.append(rel)
        return out

    def latest_release(self, r):
        skipped = set(r.get("skipped") or [])
        for rel in self.releases(r):
            if rel.get("asset") and rel["tag"] not in skipped:
                return rel
        return None

    def _newer_or_same(self, r, a, b):
        """Whether release a was published no earlier than b (the list is newest first)."""
        tags = [x["tag"] for x in r.get("releases", [])]
        if a not in tags or b not in tags:
            return False
        return tags.index(a) <= tags.index(b)

    def skip_release(self, repo_id, tag, skip=True):
        with self.lock:
            r = self.get_repo(repo_id)
            sk = [t for t in r.get("skipped", []) if t != tag]
            if skip:
                sk.append(tag)
            r["skipped"] = sk
            self.save()
        self.log("%s: %s %s" % (r["repo"], "skipping" if skip else "no longer skipping", tag))

    def check_repo(self, repo_id):
        r = self.get_repo(repo_id)
        url = "%s/repos/%s/releases?per_page=30" % (API, r["repo"])
        try:
            status, data, etag, remaining = self.http.get_json(url, r.get("etag") if r.get("releases") else None)
            self.rate_remaining = remaining
        except Exception as e:
            with self.lock:
                r["error"] = str(e)
                r["last_checked"] = now()
                self.save()
            self.log("%s: check failed: %s" % (r["repo"], e))
            return
        with self.lock:
            r["last_checked"] = now()
            r["error"] = None
            if data is not None:
                rels = []
                for rel in data:
                    if rel.get("draft"):
                        continue
                    assets = [{
                        "name": a["name"], "size": a.get("size", 0),
                        "url": a.get("browser_download_url"),
                        "digest": a.get("digest"),
                    } for a in rel.get("assets", [])]
                    item = {
                        "tag": rel.get("tag_name"), "name": rel.get("name") or rel.get("tag_name"),
                        "prerelease": bool(rel.get("prerelease")),
                        "published": rel.get("published_at") or rel.get("created_at"),
                        "body": (rel.get("body") or "")[:4000],
                        "url": rel.get("html_url"),
                        "assets": assets,
                    }
                    item["asset"] = self._pick_asset(r, assets)
                    rels.append(item)
                rels.sort(key=lambda x: x.get("published") or "", reverse=True)
                r["releases"] = rels
                r["etag"] = etag
                # First check of a new entry: learn the port name from the newest asset.
                latest = self.latest_release(r)
                if latest and not r.get("port_name"):
                    r["port_name"] = name_cleaner(latest["asset"]["name"])
                if not latest:
                    r["error"] = "No release with a matching .zip asset" if rels else "No releases yet"
            self._sync_installed(r)
            self.save()
        latest = self.latest_release(r)
        self.log("%s: latest %s%s" % (r["repo"], latest["tag"] if latest else "-",
                                       " (no change)" if data is None else ""))

    # ---------------------------------------------------------------- installed ports
    def installed_ports(self):
        """port.json files as harbourmaster wrote them, keyed by name."""
        found = {}
        try:
            entries = os.listdir(self.ports_dir)
        except OSError:
            return found
        for e in entries:
            pj = os.path.join(self.ports_dir, e, "port.json")
            if not os.path.isfile(pj):
                continue
            try:
                with open(pj, "r") as fh:
                    info = json.load(fh)
            except Exception:
                continue
            name = str(info.get("name") or "").lower()
            if not name:
                continue
            info["_dir"] = os.path.join(self.ports_dir, e)
            info["_dirname"] = e
            found[name] = info
        return found

    def _installed_info(self, r, installed):
        """The entry's installed port: by the name PortMaster recorded, else a folder named like the release zip
        (a port copied in by hand has no port.json; its version is unknown until it is installed once)."""
        names = [n for n in [r.get("port_name") or ""] + list(r.get("aliases") or []) if n]
        for name in names:
            info = installed.get(name)
            if info is not None:
                return info
        for name in names:
            if not name.endswith(".zip"):
                continue
            d = os.path.join(self.ports_dir, name[:-4])
            if os.path.isdir(d) and not os.path.isfile(os.path.join(d, "port.json")) and is_inside(d, self.ports_dir):
                return {"name": name, "_dir": d, "_dirname": os.path.basename(d), "attr": {}, "status": {},
                        "_bare": True}
        return None

    def _sync_installed(self, r, installed=None):
        """Notice installs/uninstalls done outside Slopetanium (e.g. in PortMaster)."""
        installed = self.installed_ports() if installed is None else installed
        info = self._installed_info(r, installed)
        if info is None:
            if r.get("installed_tag"):
                self.log("%s: no longer installed" % r["repo"])
            r["installed_tag"] = None
            r["installed_md5"] = None
            return None
        md5 = (info.get("status") or {}).get("md5")
        if md5 and md5 != r.get("installed_md5"):
            # Installed or replaced outside Slopetanium: a version we have cached?
            r["installed_tag"] = self._tag_for_md5(r, md5)
            r["installed_md5"] = md5
        return info

    def _tag_for_md5(self, r, md5):
        base = self._cache_dir(r)
        if os.path.isdir(base):
            for tag in os.listdir(base):
                d = os.path.join(base, tag)
                for f in os.listdir(d) if os.path.isdir(d) else []:
                    p = os.path.join(d, f)
                    if f.endswith(".zip") and file_hash(p) == md5:
                        return tag_unsafe(tag)
        return None

    def port_dir(self, r, installed=None):
        installed = self.installed_ports() if installed is None else installed
        info = self._installed_info(r, installed)
        return info["_dir"] if info else None

    # ---------------------------------------------------------------- cache
    def _cache_dir(self, r):
        return os.path.join(self.cache_dir, r["repo"].replace("/", "__"))

    def cached_tags(self, r):
        base = self._cache_dir(r)
        out = []
        if os.path.isdir(base):
            for t in os.listdir(base):
                d = os.path.join(base, t)
                if os.path.isdir(d) and any(f.endswith(".zip") for f in os.listdir(d)):
                    out.append(tag_unsafe(t))
        return out

    def _prune_cache(self, r):
        keep_n = self.settings().get("keep_versions", 2)
        keep = []
        if r.get("installed_tag"):
            keep.append(r["installed_tag"])
        for h in r.get("history", []):
            if len(keep) >= keep_n:
                break
            if h["tag"] not in keep:
                keep.append(h["tag"])
        for t in self.cached_tags(r):
            if t not in keep:
                shutil.rmtree(os.path.join(self._cache_dir(r), tag_safe(t)), ignore_errors=True)

    # ---------------------------------------------------------------- jobs
    def _worker(self):
        while not self._stop.is_set():
            try:
                job = self.job_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            job.state = "running"
            job.started = now()
            self.changed += 1
            try:
                job.fn(job)
                job.state = "done"
                if not job.message:
                    job.message = "done"
            except Exception as e:
                job.state = "failed"
                job.message = str(e) or e.__class__.__name__
                self.log("%s failed: %s" % (job.label, job.message))
                if not isinstance(e, (RuntimeError, HttpError, ValueError)):
                    traceback.print_exc()
            job.finished = now()
            job.progress = None
            self.changed += 1

    def _queue(self, kind, repo_id, label, fn):
        with self.lock:
            for j in self.jobs:
                if j.kind == kind and j.repo_id == repo_id and j.state in ("queued", "running") \
                        and kind == "check":
                    return j
            job = Job(kind, repo_id, label, fn)
            self.jobs.append(job)
            del self.jobs[:-50]
            self.changed += 1
        self.job_queue.put(job)
        return job

    def busy(self):
        return any(j.state in ("queued", "running") and j.kind != "check" for j in self.jobs)

    def repo_job(self, repo_id):
        """Running/queued job, else the last one if it failed (until the next job) or just ended."""
        j = self.active_job(repo_id)
        if j:
            return j
        for j in reversed(self.jobs):
            if j.repo_id == repo_id and j.kind != "check":
                if j.state == "failed" or (j.finished and now() - j.finished < 8):
                    return j
                return None
        return None

    def active_job(self, repo_id=None):
        for j in self.jobs:
            if j.state == "running" and (repo_id is None or j.repo_id == repo_id):
                return j
        for j in self.jobs:
            if j.state == "queued" and (repo_id is None or j.repo_id == repo_id):
                return j
        return None

    def queue_check(self, repo_id=None):
        ids = [repo_id] if repo_id else [r["id"] for r in self.state["repos"]]
        jobs = []
        for rid in ids:
            r = self.get_repo(rid)
            jobs.append(self._queue("check", rid, "Check %s" % r["repo"],
                                    lambda job, rid=rid: self.check_repo(rid)))
        return jobs

    def _auto_checker(self):
        time.sleep(2)
        first = True
        while not self._stop.is_set():
            mins = self.settings().get("auto_check_minutes", 360)
            if first or (mins and all(
                    now() - (r.get("last_checked") or 0) >= mins * 60 for r in self.state["repos"])):
                if self.state["repos"]:
                    self.queue_check()
                first = False
            self._stop.wait(30)

    def stop(self):
        self._stop.set()

    # ---------------------------------------------------------------- install
    def install_checks(self, r, tag):
        """What stands against installing now: low battery, too little space. [] when nothing does."""
        problems = []
        cap, charging = battery()
        if cap is not None and cap < LOW_BATTERY and not charging:
            problems.append("Battery at %d%%: plug in the charger, or install anyway" % cap)
        rel = self._find_release(r, tag)
        size = ((rel or {}).get("asset") or {}).get("size") or 0
        cached = os.path.isdir(os.path.join(self._cache_dir(r), tag_safe(tag)))
        free = free_space(self.ports_dir)
        # the zip (unless it is on the device already) and room to unpack it (zips of ports shrink to ~1/2)
        need = (0 if cached else size) + size * 2
        if free is not None and size and free < need:
            problems.append("Not enough space: %s free, about %s needed" % (human_size(free), human_size(need)))
        return problems

    def queue_install(self, repo_id, tag=None, force=False):
        r = self.get_repo(repo_id)
        if tag is None:
            latest = self.latest_release(r)
            if not latest:
                raise ValueError("No installable release for %s" % r["repo"])
            tag = latest["tag"]
        if not force:
            problems = self.install_checks(r, tag)
            if problems:
                raise InstallBlocked(problems)
        return self._queue("install", repo_id, "Install %s %s" % (r["repo"], tag),
                           lambda job: self._install(job, repo_id, tag))

    def queue_update_all(self, force=False):
        jobs = []
        for r in self.state["repos"]:
            latest = self.latest_release(r)
            if latest and self.has_update(r):
                jobs.append(self.queue_install(r["id"], latest["tag"], force=force))
        return jobs

    def has_update(self, r):
        latest = self.latest_release(r)
        if not latest or not r.get("port_name"):
            return False
        if self.port_dir(r) is None:
            return False  # not installed: an install, not an update
        if r.get("installed_tag") is None:
            return True  # installed version unknown
        if r["installed_tag"] == latest["tag"] or self._newer_or_same(r, r["installed_tag"], latest["tag"]):
            return False
        # Only newer releases count (a rollback leaves an "update" to the latest).
        return True

    def _find_release(self, r, tag):
        for rel in r.get("releases", []):
            if rel["tag"] == tag:
                return rel
        return None

    def _install(self, job, repo_id, tag):
        r = self.get_repo(repo_id)
        rel = self._find_release(r, tag)
        cdir = os.path.join(self._cache_dir(r), tag_safe(tag))
        zip_path = None
        if os.path.isdir(cdir):
            zs = [f for f in os.listdir(cdir) if f.endswith(".zip")]
            if zs:
                zip_path = os.path.join(cdir, zs[0])
        if zip_path is None:
            if rel is None or not rel.get("asset"):
                raise RuntimeError("Release %s has no .zip to install" % tag)
            asset = rel["asset"]
            if not os.path.isdir(cdir):
                os.makedirs(cdir)
            zip_path = os.path.join(cdir, os.path.basename(asset["name"]))
            job.message = "Downloading %s (%s)" % (asset["name"], human_size(asset.get("size")))
            self.log("%s: downloading %s %s" % (r["repo"], tag, asset["name"]))

            def prog(done, total, note=None):
                job.progress = (float(done) / total) if total else None
                job.message = note or ("Downloading %s / %s" % (human_size(done), human_size(total)))
                if note:
                    self.log("%s: %s" % (r["repo"], note))
                self.changed += 1

            try:
                self.http.download(asset["url"], zip_path, prog)
                if asset.get("size") and os.path.getsize(zip_path) != asset["size"]:
                    raise RuntimeError("Download size mismatch")
                dig = asset.get("digest") or ""
                if dig.startswith("sha256:"):
                    job.message = "Verifying download"
                    if file_hash(zip_path, "sha256") != dig.split(":", 1)[1]:
                        raise RuntimeError("Download checksum mismatch")
            except Exception:
                shutil.rmtree(cdir, ignore_errors=True)
                raise
        job.progress = None
        self._check_zip(zip_path)
        pdir = self.port_dir(r)
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            unpacked = sum(i.file_size for i in zf.infolist())
            biggest = max([i.file_size for i in zf.infolist()] or [0])
        check_fits(self.ports_dir, biggest, "A file in this release")
        free = free_space(self.ports_dir)
        if free is not None and free < unpacked:
            raise RuntimeError("Not enough space to unpack: %s free, %s needed" % (human_size(free), human_size(unpacked)))
        if pdir and os.path.realpath(pdir) != os.path.realpath(self.root):
            try:
                b = self.backup_saves(r, reason="before %s" % tag, auto=True)
                if b:
                    job.message = "Saves backed up"
            except Exception as e:
                self.log("%s: saves not backed up before installing (%s)" % (r["repo"], e))
        if pdir and os.path.realpath(pdir) == os.path.realpath(self.root):
            self._make_room_for_self_update()
        else:
            self._refuse_if_running(r)
        job.message = "Installing with PortMaster"
        self.changed += 1
        self.log("%s: installing %s" % (r["repo"], tag))
        rc, out = self.run_harbourmaster(["install", os.path.abspath(zip_path)], job)
        if rc != 0:
            raise RuntimeError("PortMaster install failed (code %s): %s" % (rc, last_line(out)))
        md5 = file_hash(zip_path)
        with self.lock:
            prev = r.get("installed_tag")
            r["port_name"] = name_cleaner(os.path.basename(zip_path))
            r["installed_tag"] = tag
            r["installed_md5"] = md5
            r["installed_at"] = now()
            hist = [h for h in r.get("history", []) if h["tag"] != tag]
            hist.insert(0, {"tag": tag, "at": now()})
            r["history"] = hist[:20]
            self._prune_cache(r)
            self.save()
        job.message = ("Installed %s" % tag) + ((" (was %s)" % prev) if prev and prev != tag else "")
        self.community.count_install(r["repo"], tag)
        if pdir and os.path.realpath(pdir) == os.path.realpath(self.root):
            job.message += ": restart Slopetanium to use it"
        self.log("%s: %s" % (r["repo"], job.message))
        if self.gog_wanted(r):
            self.log("%s: fetching its game files from GOG" % r["repo"])
            self.queue_gog(repo_id)

    def _make_room_for_self_update(self):
        """Slopetanium updating itself: its running menu binary can't be written over ("Text file busy"), but it can
        be renamed; the new one takes its name and the old one goes at the next start."""
        for f in os.listdir(self.root):
            p = os.path.join(self.root, f)
            if f.startswith("slopetanium-ui.") and not f.endswith(".old") and os.path.isfile(p):
                old = p + ".old"
                try:
                    if os.path.exists(old):
                        os.unlink(old)
                    os.rename(p, old)
                except OSError as e:
                    self.log("self-update: could not move %s aside: %s" % (f, e))

    def _check_zip(self, path):
        import zipfile
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            bad = zf.testzip()
        if bad:
            raise RuntimeError("Corrupt file in zip: %s" % bad)
        if not any(n.endswith("port.json") for n in names) and \
                not any(n.lower().endswith(".sh") and "/" not in n for n in names):
            raise RuntimeError("This zip is not a PortMaster port (no port.json / launch script)")

    def run_harbourmaster(self, args, job=None):
        if not self.harbourmaster or not os.path.isfile(self.harbourmaster):
            raise RuntimeError("PortMaster's harbourmaster not found (%s)" % self.harbourmaster)
        cmd = [self.python, self.harbourmaster, "--no-check", "--no-colour"] + list(args)
        self.log("$ harbourmaster " + " ".join(args))
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(self.harbourmaster),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = []
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            out.append(line)
            m = re.search(r"(\d+)%", line)
            if job is not None:
                if m:
                    job.progress = int(m.group(1)) / 100.0
                elif not line.startswith("- "):
                    job.message = line[:120]
                self.changed += 1
            if not line.startswith("- "):
                self.log("  " + line[:200])
        rc = proc.wait()
        return rc, out

    def running_pids(self, r):
        """Processes running from the port's folder or its launch script."""
        installed = self.installed_ports()
        info = self._installed_info(r, installed)
        if not info:
            return []
        targets = [os.path.realpath(info["_dir"])]
        for item in info.get("items", []):
            if item.lower().endswith(".sh"):
                targets.append(os.path.realpath(os.path.join(self.ports_dir, item)))
        me = os.getpid()
        found = []
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return []
        for pid in pids:
            if pid == me:
                continue
            paths = []
            try:
                paths.append(os.readlink("/proc/%d/exe" % pid))
            except OSError:
                pass
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as fh:
                    args = fh.read().split(b"\0")
                paths.extend(a.decode("utf-8", "replace") for a in args[:4] if a.startswith(b"/"))
            except (IOError, OSError):
                pass
            for pth in paths:
                rp = os.path.realpath(pth)
                if any(rp == t or rp.startswith(t + "/") for t in targets):
                    found.append(pid)
                    break
        return found

    def _refuse_if_running(self, r):
        if self.running_pids(r):
            raise RuntimeError("The port is running right now; quit it first, then try again")

    # ---------------------------------------------------------------- uninstall
    def queue_uninstall(self, repo_id, backup=True):
        r = self.get_repo(repo_id)
        return self._queue("uninstall", repo_id, "Uninstall %s" % r["repo"],
                           lambda job: self._uninstall(job, repo_id, backup))

    def _uninstall(self, job, repo_id, backup):
        r = self.get_repo(repo_id)
        pdir = self.port_dir(r)
        if not pdir:
            raise RuntimeError("%s is not installed" % r["repo"])
        if os.path.abspath(pdir) == self.root:
            raise RuntimeError("Slopetanium can't uninstall itself; use PortMaster")
        if backup:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for keep in r.get("keep_on_uninstall", []):
                src = os.path.join(pdir, keep)
                if os.path.exists(src) and is_inside(src, pdir):
                    dst = os.path.join(self.backup_dir, "%s-%s" % (os.path.basename(pdir), stamp), keep)
                    job.message = "Backing up %s" % keep
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        os.makedirs(os.path.dirname(dst))
                        shutil.copy2(src, dst)
                    self.log("%s: backed up %s to %s" % (r["repo"], keep, dst))
        self._refuse_if_running(r)
        job.message = "Uninstalling with PortMaster"
        rc, out = self.run_harbourmaster(["uninstall", r["port_name"]], job)
        if rc != 0:
            raise RuntimeError("PortMaster uninstall failed (code %s): %s" % (rc, last_line(out)))
        with self.lock:
            r["installed_tag"] = None
            r["installed_md5"] = None
            self.save()
        job.message = "Uninstalled"

    # ---------------------------------------------------------------- originals
    def originals(self, r, installed=None):
        """User-supplied original game files left in the port's import folder."""
        pdir = self.port_dir(r, installed)
        if not pdir or not r.get("originals_dir"):
            return {"dir": None, "files": [], "total": 0}
        odir = os.path.normpath(os.path.join(pdir, r["originals_dir"]))
        pats = [p.strip().lower() for p in (r.get("originals_pattern") or "").split(",") if p.strip()]
        if odir == os.path.normpath(pdir) and not pats:
            return {"dir": None, "files": [], "total": 0}  # the whole port folder is never "originals"
        files = []
        if os.path.isdir(odir) and is_inside(odir, pdir):
            for name in sorted(os.listdir(odir)):
                if any(fnmatch.fnmatch(name.lower(), k) for k in KEEP_ORIGINALS):
                    continue
                if pats and not any(fnmatch.fnmatch(name.lower(), p) for p in pats):
                    continue
                p = os.path.join(odir, name)
                if os.path.islink(p):
                    size = 0
                elif os.path.isdir(p):
                    size = dir_size(p)
                else:
                    size = os.path.getsize(p)
                files.append({"name": name, "size": size, "dir": os.path.isdir(p)})
        return {"dir": os.path.relpath(odir, self.ports_dir), "files": files,
                "total": sum(f["size"] for f in files),
                "imported_hint": self._imported_hint(pdir)}

    def _art(self, info):
        """The port's own picture: gameinfo.xml's <image>, else screenshot/cover png/jpg."""
        if not info:
            return None
        pdir = info["_dir"]
        cands = []
        gi = os.path.join(pdir, "gameinfo.xml")
        if os.path.isfile(gi):
            try:
                with open(gi, "r") as fh:
                    m = re.search(r"<image>\s*([^<]+?)\s*</image>", fh.read())
                if m:
                    cands.append(os.path.join(self.ports_dir, m.group(1)))
            except (IOError, OSError):
                pass
        for n in ("screenshot.png", "screenshot.jpg", "cover.png", "cover.jpg", "splash.png"):
            cands.append(os.path.join(pdir, n))
        for c in cands:
            if os.path.isfile(c) and is_inside(c, self.ports_dir):
                return c
        return None

    def _imported_hint(self, pdir):
        """Best guess whether the port already imported its files (data/ or game/ filled)."""
        for d in ("data", "game", "gamedata", "assets"):
            p = os.path.join(pdir, d)
            if os.path.isdir(p) and os.listdir(p):
                return d
        return None

    def delete_originals(self, repo_id, names=None):
        r = self.get_repo(repo_id)
        info = self.originals(r)
        if not info["dir"]:
            raise ValueError("No originals folder for %s" % r["repo"])
        odir = os.path.join(self.ports_dir, info["dir"])
        avail = dict((f["name"], f) for f in info["files"])
        names = list(avail) if names is None else names
        freed = 0
        for n in names:
            if n not in avail or "/" in n or n in (".", ".."):
                continue
            p = os.path.join(odir, n)
            if not is_inside(p, odir):
                continue
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
            freed += avail[n]["size"]
            self.log("%s: deleted original %s" % (r["repo"], n))
        self.changed += 1
        return freed

    def _disk(self):
        lim = fs_info(self.ports_dir)
        try:
            u = shutil.disk_usage(self.ports_dir)
            return {"free": u.free, "total": u.total, "fs": lim["fs"], "max_file": lim["max_file"]}
        except OSError:
            return {"free": None, "total": None, "fs": lim["fs"], "max_file": lim["max_file"]}

    # ---------------------------------------------------------------- backups: saves, and whole games to move
    def _backup_dir(self, r, pdir):
        d = os.path.join(self.backup_dir, os.path.basename(pdir))
        if not os.path.isdir(d):
            os.makedirs(d)
        return d

    def _save_paths(self, r, pdir):
        """The port's save and settings folders (keep_on_uninstall, conf by default) that exist."""
        out = []
        for k in r.get("keep_on_uninstall") or ["conf"]:
            p = os.path.normpath(os.path.join(pdir, k))
            if os.path.exists(p) and is_inside(p, pdir) and p != os.path.normpath(pdir):
                out.append(p)
        return out

    def _manifest(self, r, pdir, kind, reason=""):
        import platform
        return {"slopetanium_backup": 1, "repo": r["repo"], "port_name": r.get("port_name"),
                "port_dir": os.path.basename(pdir), "kind": kind, "reason": reason,
                "installed_tag": r.get("installed_tag"), "created": now(),
                "device": platform.node() or ""}

    def _write_backup(self, zf, r, pdir, kind, reason, cancel=None, progress=None):
        """Into an open ZipFile: the manifest, then the saves (kind "saves") or the whole port folder but its
        original files and leftovers (kind "full")."""
        import zipfile
        zf.writestr(BACKUP_MANIFEST, json.dumps(self._manifest(r, pdir, kind, reason), indent=1))
        if kind == "saves":
            roots = self._save_paths(r, pdir)
        else:
            roots = [pdir]
        skip_dirs = set()
        od = r.get("originals_dir") or ""
        if od and od != ".":
            skip_dirs.add(os.path.normpath(os.path.join(pdir, od)))
        pats = [p.strip().lower() for p in (r.get("originals_pattern") or "").split(",") if p.strip()]
        done = 0
        for root in roots:
            if os.path.isfile(root):
                zf.write(root, os.path.relpath(root, pdir))
                continue
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if os.path.normpath(os.path.join(dp, d)) not in skip_dirs]
                for fn in fns:
                    full = os.path.join(dp, fn)
                    rel = os.path.relpath(full, pdir)
                    low = fn.lower()
                    if os.path.islink(full) or low.endswith(".part") or (kind == "full" and (
                            low == "log.txt" or (dp == pdir and any(fnmatch.fnmatch(low, p) for p in pats)))):
                        continue
                    if cancel and cancel():
                        raise RuntimeError("cancelled")
                    zf.write(full, rel, compress_type=zipfile.ZIP_STORED if kind == "full" else zipfile.ZIP_DEFLATED)
                    done += os.path.getsize(full)
                    if progress:
                        progress(done)
        return done

    def backup_saves(self, r, reason="", auto=False):
        """A zip of the port's saves on the handheld (conf/backups/<port>/); the last 5 automatic ones are kept."""
        import zipfile
        pdir = self.port_dir(r)
        if not pdir:
            raise ValueError("%s is not installed" % r["repo"])
        if not self._save_paths(r, pdir):
            return None
        d = self._backup_dir(r, pdir)
        name = "%s-%s%s.zip" % (time.strftime("%Y%m%d-%H%M%S"), "auto" if auto else "saves", "")
        path = os.path.join(d, name)
        with zipfile.ZipFile(path + ".part", "w", zipfile.ZIP_DEFLATED) as zf:
            self._write_backup(zf, r, pdir, "saves", reason)
        os.replace(path + ".part", path)
        if auto:
            autos = sorted(f for f in os.listdir(d) if f.endswith("-auto.zip"))
            for old in autos[:-5]:
                os.unlink(os.path.join(d, old))
        self.log("%s: saves backed up (%s)" % (r["repo"], name))
        self.changed += 1
        return path

    def backups(self, r, pdir=None):
        pdir = pdir or self.port_dir(r)
        if not pdir:
            return []
        d = os.path.join(self.backup_dir, os.path.basename(pdir))
        out = []
        if os.path.isdir(d):
            for f in sorted(os.listdir(d), reverse=True):
                if f.endswith(".zip"):
                    p = os.path.join(d, f)
                    out.append({"name": f, "size": os.path.getsize(p), "at": int(os.path.getmtime(p)),
                                "auto": f.endswith("-auto.zip"), "kind": "full" if f.endswith("-full.zip") else "saves"})
        return out

    def backup_file(self, repo_id, name):
        r = self.get_repo(repo_id)
        pdir = self.port_dir(r)
        if not pdir or "/" in name or not name.endswith(".zip"):
            raise ValueError("No such backup")
        p = os.path.join(self.backup_dir, os.path.basename(pdir), name)
        if not os.path.isfile(p):
            raise ValueError("No such backup")
        return p

    def delete_backup(self, repo_id, name):
        os.unlink(self.backup_file(repo_id, name))
        self.changed += 1

    def stream_backup(self, repo_id, kind, out):
        """A backup written straight to out (the browser's download): nothing stored on the handheld."""
        import zipfile
        r = self.get_repo(repo_id)
        pdir = self.port_dir(r)
        if not pdir:
            raise ValueError("%s is not installed" % r["repo"])
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            n = self._write_backup(zf, r, pdir, kind, "download")
        self.log("%s: %s backup downloaded (%s)" % (r["repo"], kind, human_size(n)))

    def backup_estimate(self, r, pdir):
        """Bytes a whole-game backup holds (no original files)."""
        total = dir_size(pdir)
        o = self.originals(r)
        return max(0, total - (o.get("total") or 0))

    def restore_backup(self, repo_id, path):
        """A backup zip unpacked into the port's folder: checked first (a Slopetanium backup of this port, no path
        outside the folder); the current saves are backed up before."""
        import zipfile
        r = self.get_repo(repo_id)
        pdir = self.port_dir(r)
        if not pdir:
            raise ValueError("Install %s first, then restore into it" % r["repo"])
        self._refuse_if_running(r)
        with zipfile.ZipFile(path) as zf:
            try:
                man = json.loads(zf.read(BACKUP_MANIFEST).decode("utf-8"))
            except KeyError:
                raise ValueError("This zip is not a Slopetanium backup")
            if man.get("repo", "").lower() != r["repo"].lower():
                raise ValueError("This backup is of %s, not %s" % (man.get("repo"), r["repo"]))
            names = [n for n in zf.namelist() if n != BACKUP_MANIFEST]
            for n in names:
                t = os.path.normpath(os.path.join(pdir, n))
                if n.startswith("/") or not is_inside(t, pdir):
                    raise ValueError("Unsafe path in the backup: %s" % n)
            try:
                self.backup_saves(r, reason="before restoring %s" % os.path.basename(path), auto=True)
            except Exception as e:
                self.log("%s: saves not backed up before restoring (%s)" % (r["repo"], e))
            for info in zf.infolist():
                if info.filename == BACKUP_MANIFEST:
                    continue
                zf.extract(info, pdir)
        self.log("%s: restored %s (%s from %s)" % (r["repo"], os.path.basename(path), man.get("kind"),
                                                    man.get("device") or "?"))
        self.changed += 1
        return man

    def queue_restore(self, repo_id, path):
        r = self.get_repo(repo_id)
        return self._queue("restore", repo_id, "Restore %s" % r["repo"],
                           lambda job: self._restore_job(job, repo_id, path))

    def _restore_job(self, job, repo_id, path):
        job.message = "Restoring %s" % os.path.basename(path)
        man = self.restore_backup(repo_id, path)
        job.message = "Restored %s backup%s" % (man.get("kind"), (" from " + man["device"]) if man.get("device") else "")

    def import_status(self, repo_id, name, total):
        path = self.import_path(repo_id, name)
        part = path + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if have > total:
            os.unlink(part)
            have = 0
        lim = fs_info(os.path.dirname(path))
        return {"have": have, "done": False, "free": free_space(os.path.dirname(path)),
                "saved_as": os.path.basename(path), "fs": lim["fs"], "max_file": lim["max_file"]}

    def import_chunk(self, repo_id, name, offset, total, read, length):
        path = self.import_path(repo_id, name)
        check_fits(os.path.dirname(path), total, "This backup")
        part = path + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if offset != have:
            raise UploadOffset(have)
        if have + length > total:
            raise ValueError("More data than the file's size")
        with open(part, "ab") as fh:
            left = length
            while left > 0:
                buf = read(min(left, 1 << 20))
                if not buf:
                    raise IOError("upload cut off")
                fh.write(buf)
                left -= len(buf)
        have += length
        done = have == total
        if done:
            os.replace(part, path)
            self.changed += 1
        return {"have": have, "done": done, "saved_as": os.path.basename(path)}

    def import_path(self, repo_id, name):
        """Where an uploaded backup zip lands (then restored from there)."""
        r = self.get_repo(repo_id)
        pdir = self.port_dir(r)
        if not pdir:
            raise ValueError("Install %s first, then restore into it" % r["repo"])
        base = os.path.basename((name or "").replace("\\", "/"))
        if not base.lower().endswith(".zip") or base.startswith("."):
            raise ValueError("A backup is a .zip")
        d = self._backup_dir(r, pdir)
        stem = base[:-4]
        if not stem.endswith(("-full", "-saves", "-auto")):
            stem += "-imported"
        return os.path.join(d, stem + ".zip")

    # ---------------------------------------------------------------- box art
    def _cover_path(self, r):
        """Where a port's box art is kept: from its own cover_url, or else from the community server (which has the
        game's cover from IGDB for ports shared there)."""
        u = r.get("cover_url") or ""
        base = os.path.join(self.conf_dir, "art", r["repo"].replace("/", "__"))
        if not u:
            return base + ".community.jpg"
        ext = os.path.splitext(u.split("?")[0])[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            ext = ".jpg"
        return base + ext

    def _cover_source(self, r):
        """(url, retry seconds) of a port's box art."""
        if r.get("cover_url"):
            return r["cover_url"], 3600
        return "%s/cover/%s" % (self.community.url, r["repo"]), 6 * 3600  # (only shared ports have one there)

    def cover(self, r, info=None):
        """The box art on the device: the fetched one; Slopetanium's own; else nothing (the port's screenshot then)."""
        p = self._cover_path(r)
        if p and os.path.isfile(p):
            return p
        if info and os.path.realpath(info["_dir"]) == os.path.realpath(self.root):
            own = os.path.join(self.root, "assets", "icons", "slopetanium-cover.png")
            if os.path.isfile(own):
                return own
        return None

    def _art_fetcher(self):
        """Box art for the watched ports, once each (a failure is tried again in an hour)."""
        tried = self._art_tried = {}
        while not self._stop.is_set():
            for r in list(self.state["repos"]):
                p = self._cover_path(r)
                if not p or os.path.isfile(p):
                    continue
                url, again = self._cover_source(r)
                if now() - tried.get(p, 0) < again:
                    continue
                tried[p] = now()
                try:
                    if not os.path.isdir(os.path.dirname(p)):
                        os.makedirs(os.path.dirname(p))
                    self.http.download(url, p, attempts=2)
                    if os.path.getsize(p) < 512:
                        os.unlink(p)
                        raise RuntimeError("not a picture")
                    self.log("%s: box art fetched" % r["repo"])
                    self.changed += 1
                except Exception as e:
                    if r.get("cover_url") or not (isinstance(e, HttpError) and e.status == 404):
                        self.log("%s: box art not fetched (%s)" % (r["repo"], e))
            self._stop.wait(20)

    # ---------------------------------------------------------------- uploads (game files from the browser)
    def upload_dest(self, r):
        pdir = self.port_dir(r)
        if not pdir:
            raise ValueError("Install the port first: its game files go into its folder")
        od = r.get("originals_dir") or ""
        if not od:
            raise ValueError("This port takes no game files")
        dest = os.path.normpath(os.path.join(pdir, od))
        if not is_inside(dest, pdir):
            raise ValueError("Bad game files folder")
        if not os.path.isdir(dest):
            os.makedirs(dest)
        return dest

    def _upload_path(self, r, name, single=False, into=None):
        """Where an uploaded file goes: its name (a folder upload: its relative path) inside the game files folder,
        or inside into (a folder of the port, relative to it: the file browser)."""
        name = (name or "").replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise ValueError("Bad file name")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." or p.startswith("\0") for p in parts) or any("\0" in p for p in parts):
            raise ValueError("Bad file name")
        if single and len(parts) == 1:
            for pat, new in (r.get("rename") or {}).items():
                if fnmatch.fnmatch(parts[0].lower(), pat.lower()):
                    parts = [new]
                    break
        if into is not None:
            dest = self.fs_path(r, into)
            if not os.path.isdir(dest):
                raise ValueError("No such folder: %s" % (into or "/"))
            single = False
        else:
            dest = self.upload_dest(r)
        path = os.path.normpath(os.path.join(dest, *parts))
        if not is_inside(path, dest) or path == dest:
            raise ValueError("Bad file name")
        return dest, path

    def upload_status(self, repo_id, name, total, single=False, into=None):
        r = self.get_repo(repo_id)
        dest, path = self._upload_path(r, name, single, into)
        lim = fs_info(dest)
        try:
            free = shutil.disk_usage(dest).free
        except OSError:
            free = None
        extra = {"fs": lim["fs"], "max_file": lim["max_file"]}
        if into is None and os.path.isfile(path) and os.path.getsize(path) == total:
            return dict(extra, have=total, done=True, free=free, saved_as=os.path.relpath(path, self.ports_dir))
        part = path + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if have > total:
            os.unlink(part)
            have = 0
        return dict(extra, have=have, done=False, free=free, saved_as=os.path.relpath(path, self.ports_dir))

    def upload_chunk(self, repo_id, name, offset, total, read, length, single=False, into=None):
        """Appends length bytes (read(n) gives them) at offset to the file's .part; the last one completes it."""
        r = self.get_repo(repo_id)
        dest, path = self._upload_path(r, name, single, into)
        check_fits(dest, total, os.path.basename(path))
        part = path + ".part"
        if not os.path.isdir(os.path.dirname(part)):
            os.makedirs(os.path.dirname(part))
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if offset != have:
            raise UploadOffset(have)
        if have + length > total:
            raise ValueError("More data than the file's size")
        with open(part, "ab") as fh:
            left = length
            while left > 0:
                buf = read(min(left, 1 << 20))
                if not buf:
                    raise IOError("upload cut off")
                fh.write(buf)
                left -= len(buf)
        have += length
        done = have == total
        if done:
            if os.path.isdir(path):
                raise ValueError("A folder is in the way: %s" % os.path.basename(path))
            os.replace(part, path)
            self.log("%s: %s %s uploaded (%s)" % (r["repo"], "file" if into is not None else "game file",
                                                 os.path.relpath(path, self.port_dir(r) if into is not None else dest),
                                                 human_size(total)))
            self.changed += 1
        return {"have": have, "done": done}

    # ---------------------------------------------------------------- the file browser (one port's folder)
    def fs_path(self, r, rel):
        """A path inside the port's folder from one relative to it; symlinks are followed and must stay inside."""
        pdir = self.port_dir(r)
        if not pdir:
            raise ValueError("%s is not installed" % r["repo"])
        rel = (rel or "").replace("\\", "/").strip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." or "\0" in p for p in parts):
            raise ValueError("Bad path")
        path = os.path.normpath(os.path.join(pdir, *parts)) if parts else pdir
        if not is_inside(path, pdir):
            raise ValueError("That is outside the port's folder")
        return path

    def _fs_rel(self, r, path):
        rel = os.path.relpath(path, self.port_dir(r))
        return "" if rel == "." else rel.replace(os.sep, "/")

    def fs_list(self, repo_id, rel=""):
        r = self.get_repo(repo_id)
        pdir = self.port_dir(r)
        path = self.fs_path(r, rel)
        if not os.path.isdir(path):
            raise ValueError("No such folder: %s" % (rel or "/"))
        out = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
            except OSError:  # a dangling link
                out.append({"name": name, "dir": False, "size": 0, "mtime": 0, "outside": True})
                continue
            isdir = os.path.isdir(full)
            e = {"name": name, "dir": isdir, "size": 0 if isdir else st.st_size, "mtime": int(st.st_mtime)}
            if os.path.islink(full) and not is_inside(full, pdir):
                e["outside"] = True
            elif not isdir:
                e["text"] = _looks_text(full, st.st_size)
            out.append(e)
        out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        lim = fs_info(path)
        return {"path": self._fs_rel(r, path), "root": os.path.basename(pdir), "entries": out,
                "free": free_space(path), "fs": lim["fs"], "max_file": lim["max_file"],
                "running": bool(self.running_pids(r))}

    def fs_file(self, repo_id, rel):
        """The real path of a file of the port, for a download."""
        r = self.get_repo(repo_id)
        path = self.fs_path(r, rel)
        if not os.path.exists(path):
            raise ValueError("No such file: %s" % rel)
        return r, path

    def fs_read(self, repo_id, rel):
        r, path = self.fs_file(repo_id, rel)
        if os.path.isdir(path):
            raise ValueError("That is a folder")
        size = os.path.getsize(path)
        if size > TEXT_MAX:
            raise ValueError("Too big to edit here (%s); download it instead" % human_size(size))
        with open(path, "rb") as fh:
            data = fh.read()
        if b"\0" in data:
            raise ValueError("Not a text file; download it instead")
        try:
            text, enc = data.decode("utf-8"), "utf-8"
            if text.startswith("\ufeff"):
                text, enc = text[1:], "utf-8-sig"
        except UnicodeDecodeError:
            text, enc = data.decode("latin-1"), "latin-1"
        crlf = b"\r\n" in data
        return {"path": self._fs_rel(r, path), "text": text.replace("\r\n", "\n") if crlf else text,
                "encoding": enc, "crlf": crlf, "mtime": os.stat(path).st_mtime, "size": size}

    def fs_write(self, repo_id, rel, text, mtime=None, encoding="utf-8", crlf=False):
        """Saves an edited text file in its own encoding and line endings; mtime: the version the editor started
        from (another change since then: refused, unless None)."""
        r = self.get_repo(repo_id)
        path = self.fs_path(r, rel)
        if not os.path.isdir(os.path.dirname(path)):
            raise ValueError("No such folder")
        if os.path.isdir(path):
            raise ValueError("That is a folder")
        if mtime is not None and os.path.exists(path) and abs(os.stat(path).st_mtime - float(mtime)) > 1e-3:
            raise FsConflict("The file changed on the handheld since you opened it")
        if encoding not in ("utf-8", "utf-8-sig", "latin-1"):
            encoding = "utf-8"
        if crlf:
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        try:
            data = text.encode(encoding)
        except UnicodeEncodeError:
            data = text.encode("utf-8")
        tmp = path + ".slop-tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        if os.path.exists(path):
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
        self.log("%s: %s edited" % (r["repo"], self._fs_rel(r, path)))
        return {"mtime": os.stat(path).st_mtime, "size": len(data)}

    def fs_mkdir(self, repo_id, rel, name):
        r = self.get_repo(repo_id)
        base = self.fs_path(r, rel)
        path = self.fs_path(r, (rel.rstrip("/") + "/" if rel else "") + _plain_name(name))
        if os.path.exists(path):
            raise ValueError("%s already exists" % name)
        if not os.path.isdir(base):
            raise ValueError("No such folder")
        os.mkdir(path)
        return {"path": self._fs_rel(r, path)}

    def fs_rename(self, repo_id, rel, name):
        r = self.get_repo(repo_id)
        src = self.fs_path(r, rel)
        if src == self.port_dir(r):
            raise ValueError("The port's own folder can't be renamed here")
        if not os.path.lexists(src):
            raise ValueError("No such file: %s" % rel)
        dst = os.path.join(os.path.dirname(src), _plain_name(name))
        if not is_inside(dst, self.port_dir(r)):
            raise ValueError("Bad name")
        if os.path.lexists(dst):
            raise ValueError("%s already exists" % name)
        os.rename(src, dst)
        self.log("%s: %s renamed to %s" % (r["repo"], rel, os.path.basename(dst)))
        return {"path": self._fs_rel(r, dst)}

    def fs_delete(self, repo_id, rels):
        r = self.get_repo(repo_id)
        pdir = self.port_dir(r)
        freed = 0
        paths = []
        for rel in rels or []:
            # a link is removed as itself (lexists): never what it points to
            parent = self.fs_path(r, os.path.dirname(rel.strip("/")))
            path = os.path.join(parent, _plain_name(os.path.basename(rel.strip("/"))))
            if os.path.realpath(parent) == os.path.realpath(pdir) and os.path.basename(path) == "":
                raise ValueError("The port's own folder can't be deleted here")
            if not os.path.lexists(path):
                raise ValueError("No such file: %s" % rel)
            paths.append(path)
        for path in paths:
            if os.path.isdir(path) and not os.path.islink(path):
                freed += dir_size(path)
                shutil.rmtree(path)
            else:
                freed += os.lstat(path).st_size
                os.unlink(path)
        self.log("%s: deleted %s (%s)" % (r["repo"], ", ".join(rels), human_size(freed)))
        self.changed += 1
        return freed

    def fs_zip(self, repo_id, rel, out):
        """A folder of the port, zipped while it streams to out."""
        import zipfile
        r = self.get_repo(repo_id)
        pdir = self.port_dir(r)
        base = self.fs_path(r, rel)
        if not os.path.isdir(base):
            raise ValueError("No such folder")
        top = os.path.basename(base)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if is_inside(os.path.join(root, d), pdir)]
                for f in files:
                    full = os.path.join(root, f)
                    if not is_inside(full, pdir) or not os.path.isfile(full):
                        continue
                    zf.write(full, os.path.join(top, os.path.relpath(full, base)))

    # ---------------------------------------------------------------- GOG
    def _gog_files_present(self, r, pdir=None):
        g = r.get("gog") or {}
        pdir = pdir or self.port_dir(r)
        if not g or not pdir:
            return False
        dest = os.path.join(pdir, g.get("dest") or "source")
        pats = [p.strip().lower() for p in (g.get("files") or "*.exe").split(",") if p.strip()]
        try:
            names = os.listdir(dest)
        except OSError:
            return False
        return any(any(fnmatch.fnmatch(n.lower(), p) for p in pats) and not n.endswith(".part") for n in names)

    def gog_owned(self, r):
        """True / False when the library is known, None while it isn't (the snapshot never waits for GOG)."""
        g = r.get("gog")
        if not g or not self.gog.linked() or self.gog._owned is None:
            return None
        return int(g["id"]) in self.gog._owned

    def gog_wanted(self, r):
        """After an install: fetch the game files when the user wants that, owns the game, and the port has neither
        its installer nor imported data yet."""
        if not r.get("gog") or not self.settings().get("gog_auto", True) or not self.gog.linked():
            return False
        pdir = self.port_dir(r)
        if not pdir or self._imported_hint(pdir) or self._gog_files_present(r, pdir):
            return False
        try:
            return int(r["gog"]["id"]) in self.gog.owned()
        except Exception as e:
            self.log("GOG: %s" % e)
            return False

    def queue_gog(self, repo_id):
        r = self.get_repo(repo_id)
        if not r.get("gog"):
            raise ValueError("%s has no GOG game set" % r["repo"])
        return self._queue("gog", repo_id, "GOG %s" % (r["gog"].get("title") or r["repo"]),
                           lambda job: self._gog_download(job, repo_id))

    def _gog_download(self, job, repo_id):
        r = self.get_repo(repo_id)
        g = r["gog"]
        pdir = self.port_dir(r)
        if not pdir:
            raise RuntimeError("Install the port first: its game files go into its folder")
        if not self.gog.linked():
            raise RuntimeError("Sign in to GOG first (web interface: Settings)")
        job.message = "Checking your GOG library"
        if int(g["id"]) not in self.gog.owned(max_age=0):
            raise RuntimeError("%s is not in your GOG library" % (g.get("title") or "This game"))
        parts = self.gog.installers(g["id"])
        if not parts:
            raise RuntimeError("GOG lists no Windows installer for %s" % (g.get("title") or g["id"]))
        dest = os.path.join(pdir, g.get("dest") or "source")
        if not is_inside(dest, pdir):
            raise RuntimeError("Bad destination folder")
        got = []
        for i, part in enumerate(parts):
            label = "%s (%d/%d)" % (g.get("title") or "game", i + 1, len(parts)) if len(parts) > 1 else (g.get("title") or "game")

            def prog(done, total, note=None, label=label):
                job.progress = (float(done) / total) if total else None
                job.message = note or ("GOG: %s %s / %s" % (label, human_size(done), human_size(total)))
                self.changed += 1
            path = self.gog.download(part["url"], dest, prog)
            got.append(os.path.basename(path))
            self.log("%s: GOG file %s" % (r["repo"], os.path.basename(path)))
        job.progress = None
        job.message = "Game files ready in %s: start the port to import them" % os.path.relpath(dest, self.ports_dir)
        self.changed += 1

    def _gog_refresher(self):
        """The library, now and then, so the menus know what the account owns without waiting for GOG."""
        while not self._stop.is_set():
            if self.gog.linked() and any(r.get("gog") for r in self.state["repos"]):
                try:
                    self.gog.owned(max_age=900)
                    self.changed += 1
                except Exception as e:
                    self.log("GOG library: %s" % e)
            self._stop.wait(60)

    def gog_link(self, pasted):
        st = self.gog.link(pasted)
        try:
            self.gog.owned(max_age=0)
        except Exception:
            pass
        self.changed += 1
        return st

    def gog_unlink(self):
        self.gog.unlink()
        self.changed += 1

    # ---------------------------------------------------------------- views
    def repo_view(self, r, installed=None):
        installed = self.installed_ports() if installed is None else installed
        info = self._installed_info(r, installed)
        latest = self.latest_release(r)
        cached = self.cached_tags(r)
        job = self.repo_job(r["id"])
        if info is None:
            status = "not_installed"
        elif r.get("installed_tag") is None:
            status = "unknown_version"
        elif latest and r["installed_tag"] != latest["tag"] and not self._newer_or_same(r, r["installed_tag"], latest["tag"]):
            status = "update"
        else:
            status = "current"
        if r.get("error") and not latest:
            status = "error"
        rels = []
        for rel in self.releases(r):
            rels.append({
                "tag": rel["tag"], "name": rel["name"], "prerelease": rel["prerelease"],
                "published": rel["published"], "url": rel.get("url"), "body": rel.get("body", ""),
                "asset": rel.get("asset"), "cached": rel["tag"] in cached,
                "installed": rel["tag"] == r.get("installed_tag") and info is not None,
                "skipped": rel["tag"] in (r.get("skipped") or []),
            })
        prev = None
        for h in r.get("history", []):
            if h["tag"] != r.get("installed_tag"):
                prev = h["tag"]
                break
        return {
            "id": r["id"], "repo": r["repo"], "asset_pattern": r["asset_pattern"],
            "prereleases": r["prereleases"], "port_name": r["port_name"],
            "originals_dir": r["originals_dir"], "keep_on_uninstall": r["keep_on_uninstall"],
            "title": ((info or {}).get("attr") or {}).get("title") or r.get("title") or r["repo"].split("/")[1],
            "is_self": bool(info and os.path.realpath(info["_dir"]) == os.path.realpath(self.root)),
            "installed": info is not None, "installed_tag": r.get("installed_tag"),
            "installed_at": r.get("installed_at"),
            "port_dir": info["_dirname"] if info else None,
            "latest_tag": latest["tag"] if latest else None,
            "latest_published": latest["published"] if latest else None,
            "is_new": bool(latest and r.get("seen_tag") != latest["tag"] and status == "update"),
            "status": status, "error": r.get("error"), "last_checked": r.get("last_checked"),
            "previous_tag": prev, "releases": rels,
            "art": self._art(info),
            "cover": self.cover(r, info), "cover_url": r.get("cover_url", ""),
            "skipped": r.get("skipped", []),
            "shared": bool(r.get("shared")),
            "upload_fs": fs_info(os.path.join(info["_dir"], r.get("originals_dir") or ".")) if info else None,
            "backup_fs": fs_info(self.backup_dir),
            "backups": self.backups(r, info["_dir"]) if info else [],
            "originals_pattern": r.get("originals_pattern", ""), "aliases": r.get("aliases", []),
            "hint": r.get("hint", ""), "buy": r.get("buy", []),
            "upload_dir": (os.path.relpath(os.path.normpath(os.path.join(info["_dir"], r["originals_dir"])), self.ports_dir)
                           if info and r.get("originals_dir") else None),
            "gog": (dict(r["gog"], owned=self.gog_owned(r),
                         present=self._gog_files_present(r, info["_dir"] if info else None))
                    if r.get("gog") else None),
            "size": (latest.get("asset") or {}).get("size") if latest else None,
            "job": job.to_dict() if job else None,
        }

    def snapshot(self):
        with self.lock:
            installed = self.installed_ports()
            for r in self.state["repos"]:
                self._sync_installed(r, installed)
            repos = [self.repo_view(r, installed) for r in self.state["repos"]]
            for rv, r in zip(repos, self.state["repos"]):
                o = self.originals(r, installed)
                rv["originals"] = o
            watched = set(r["port_name"] for r in self.state["repos"] if r.get("port_name"))
            others = sorted(
                [{"name": k, "title": (v.get("attr") or {}).get("title") or k, "dir": v["_dirname"]}
                 for k, v in installed.items() if k not in watched],
                key=lambda x: x["title"].lower())
            s = dict(self.state["settings"])
            s["github_token"] = "set" if s.get("github_token") else ""
            s.pop("pin", None)
            s.pop("gog", None)  # its token never leaves the core
            s.pop("device_secret", None)  # nor the handheld's community id
            s["community_url"] = self.community.url
            return {
                "repos": repos,
                "other_ports": others,
                "settings": s,
                "jobs": [j.to_dict() for j in self.jobs[-20:]][::-1],
                "log": self.log_lines[-120:],
                "busy": self.busy(),
                "rate_remaining": self.rate_remaining,
                "ports_dir": self.ports_dir,
                "controlfolder": self.controlfolder,
                "changed": self.changed,
                "gog_account": self.gog.status(),
                "battery": dict(zip(("percent", "charging"), battery())),
                "disk": self._disk(),
                "low_battery": LOW_BATTERY,
                "catalogue": self.catalogue(),
            }


def tag_safe(tag):
    return re.sub(r"[^A-Za-z0-9._+-]", lambda m: "%%%02X" % ord(m.group(0)), tag)


def tag_unsafe(name):
    return re.sub(r"%([0-9A-F]{2})", lambda m: chr(int(m.group(1), 16)), name)


def is_inside(path, base):
    path = os.path.realpath(path)
    base = os.path.realpath(base)
    return path == base or path.startswith(base.rstrip("/") + "/")


def last_line(lines):
    for l in reversed(lines):
        if l.strip() and not l.startswith("- "):
            return l.strip()
    return ""
