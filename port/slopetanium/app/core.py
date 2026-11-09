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

USER_AGENT = "slopetanium/0.1"
API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# The ports Slopetanium knows from the start (rzrbld's recompiled ports, and itself). Added to the watch list once
# (settings["catalogue_offered"] remembers which were offered, so a removed one stays removed).
#   originals_dir / originals_pattern: where the user's own game files go ("." = the port folder itself)
#   aliases: other names the port's port.json may carry (a zip installed by hand)
#   gog: the game on GOG.com: its product id, where its installer goes, and the installer's file names
CATALOGUE = [
    {"repo": "rzrbld/ReNFS5", "hint": "Your installed game folder (Porsche.exe, Drivers/dx7z.dll, FEData/, GameData/), or a ZIP of it.", "title": "ReNFS5 (Need for Speed: Porsche Unleashed)", "originals_dir": "source"},
    {"repo": "rzrbld/The-ReThing", "hint": "Your game ISO (it is saved as the_thing.iso).", "rename": {"*.iso": "the_thing.iso"}, "title": "The ReThing (The Thing)", "originals_dir": "source"},
    {"repo": "rzrbld/ReConflict", "hint": "The GOG installer, setup_conflict_-_desert_storm_1.0_(21657).exe, with its .bin parts if it has any.", "buy": [{"store": "GOG.com", "url": "https://www.gog.com/en/game/conflict_desert_storm"}], "title": "ReConflict (Conflict: Desert Storm)", "originals_dir": ".", "originals_pattern": "setup_*.exe,setup_*.bin",
     "gog": {"id": 1207658998, "title": "Conflict: Desert Storm", "dest": ".", "files": "setup_conflict*"}},
    {"repo": "rzrbld/vurus", "hint": "Your original disc image: an ISO, or a single-BIN CUE/BIN.", "title": "Vurus (Vyruz)", "originals_dir": ".", "originals_pattern": "*.iso,*.cue,*.bin", "aliases": ["vurus.zip"]},
    {"repo": "rzrbld/TheFamilia", "hint": "The three Mafia CD images (.bin+.cue or .iso), mafia_patch_1.2_eng.exe and an unprotected 1.2 Game.exe (loose or in a .zip/.rar/.7z).", "title": "The Familia (Mafia)", "originals_dir": "source"},
    {"repo": "rzrbld/ReRED_Faction_Duo", "hint": "The GOG installer, setup_red_faction2_2.0.0.6.exe.", "buy": [{"store": "GOG.com", "url": "https://www.gog.com/en/game/red_faction_2"}], "title": "ReRed Faction II", "originals_dir": "source",
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
        if catalogue:
            self._seed_catalogue()
        self._stop = threading.Event()
        threading.Thread(target=self._worker, name="jobs", daemon=True).start()
        threading.Thread(target=self._auto_checker, name="autocheck", daemon=True).start()
        threading.Thread(target=self._gog_refresher, name="gog", daemon=True).start()

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
        for r in state["repos"]:
            self._repo_defaults(r)
        return state

    def _repo_defaults(self, r):
        r.setdefault("asset_pattern", "")
        r.setdefault("prereleases", True)
        r.setdefault("port_name", "")  # harbourmaster's name: the zip file name, lower case
        r.setdefault("installed_tag", None)
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
        return r

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
                        for k in ("title", "hint", "buy", "rename", "aliases", "gog", "originals_pattern"):
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
                for k in ("title", "originals_dir", "originals_pattern", "aliases", "gog", "hint", "buy", "rename"):
                    if k in c:
                        r[k] = c[k]
                self.state["repos"].append(r)
                added.append(c["repo"])
            self.save()
        for a in added:
            self.log("watching %s (catalogue)" % a)

    def catalogue(self):
        watched = set(r["repo"].lower() for r in self.state["repos"])
        return [{"repo": c["repo"], "watched": c["repo"].lower() in watched, "gog": bool(c.get("gog"))}
                for c in CATALOGUE]

    def add_from_catalogue(self, repo):
        for c in CATALOGUE:
            if c["repo"].lower() == repo.lower():
                data = {"repo": c["repo"]}
                for k in ("title", "originals_dir", "originals_pattern", "aliases", "gog", "hint", "buy", "rename"):
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
        for rel in self.releases(r):
            if rel.get("asset"):
                return rel
        return None

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
    def queue_install(self, repo_id, tag=None):
        r = self.get_repo(repo_id)
        if tag is None:
            latest = self.latest_release(r)
            if not latest:
                raise ValueError("No installable release for %s" % r["repo"])
            tag = latest["tag"]
        return self._queue("install", repo_id, "Install %s %s" % (r["repo"], tag),
                           lambda job: self._install(job, repo_id, tag))

    def queue_update_all(self):
        jobs = []
        for r in self.state["repos"]:
            latest = self.latest_release(r)
            if latest and self.has_update(r):
                jobs.append(self.queue_install(r["id"], latest["tag"]))
        return jobs

    def has_update(self, r):
        latest = self.latest_release(r)
        if not latest or not r.get("port_name"):
            return False
        if self.port_dir(r) is None:
            return False  # not installed: an install, not an update
        if r.get("installed_tag") is None:
            return True  # installed version unknown
        if r["installed_tag"] == latest["tag"]:
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

    def _upload_path(self, r, name, single=False):
        """Where an uploaded file goes: its name (a folder upload: its relative path) inside the game files folder."""
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
        dest = self.upload_dest(r)
        path = os.path.normpath(os.path.join(dest, *parts))
        if not is_inside(path, dest) or path == dest:
            raise ValueError("Bad file name")
        return dest, path

    def upload_status(self, repo_id, name, total, single=False):
        r = self.get_repo(repo_id)
        dest, path = self._upload_path(r, name, single)
        try:
            free = shutil.disk_usage(dest).free
        except OSError:
            free = None
        if os.path.isfile(path) and os.path.getsize(path) == total:
            return {"have": total, "done": True, "free": free, "saved_as": os.path.relpath(path, self.ports_dir)}
        part = path + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if have > total:
            os.unlink(part)
            have = 0
        return {"have": have, "done": False, "free": free, "saved_as": os.path.relpath(path, self.ports_dir)}

    def upload_chunk(self, repo_id, name, offset, total, read, length, single=False):
        """Appends length bytes (read(n) gives them) at offset to the file's .part; the last one completes it."""
        r = self.get_repo(repo_id)
        dest, path = self._upload_path(r, name, single)
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
            os.replace(part, path)
            self.log("%s: game file %s uploaded (%s)" % (r["repo"], os.path.relpath(path, dest), human_size(total)))
            self.changed += 1
        return {"have": have, "done": done}

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
        elif latest and r["installed_tag"] != latest["tag"]:
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
