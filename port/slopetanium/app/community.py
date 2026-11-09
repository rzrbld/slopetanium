# SPDX-License-Identifier: MIT
"""The community at slopetanium.pw: ports Slopetanium users share, their reviews, and install counts.

The handheld is known to the server only by a random secret made here (settings["device_secret"]); the server keeps
a keyed hash of it, no name or address. The secret never leaves the core: the browser talks to the core, the core
talks to the server. Installs are counted only after the owner said yes (settings["count_installs"] is True).
Python 3.7 compatible.
"""

import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

try:
    import requests  # PortMaster's exlibs (certifi with it: HTTPS works on CFWs without a CA bundle)
except Exception:  # pragma: no cover
    requests = None

DEFAULT_URL = "https://slopetanium.pw"
UA = "slopetanium (community)"
# what a shared port carries for other handhelds: how to install it well, nothing about this one
SHARED_FIELDS = ("originals_dir", "originals_pattern", "keep_on_uninstall", "asset_pattern", "prereleases", "gog",
                 "hint", "buy", "rename")


class CommunityError(Exception):
    def __init__(self, status, msg):
        Exception.__init__(self, msg)
        self.status = status


def device_name():
    """The handheld's model, as PortMaster names it (or the device tree's)."""
    n = (os.environ.get("DEVICE_NAME") or "").strip()
    if not n:
        try:
            with open("/proc/device-tree/model", "rb") as fh:
                n = fh.read().rstrip(b"\0").decode("utf-8", "replace").strip()
        except (IOError, OSError):
            n = ""
    return n[:40]


def cfw_name():
    return (os.environ.get("CFW_NAME") or "").strip()[:40]


class Community(object):
    def __init__(self, settings, save, log):
        self.settings = settings
        self.save = save
        self.log = log
        self._sync_lock = threading.Lock()
        if not self.settings.get("device_secret"):
            self.settings["device_secret"] = secrets.token_hex(32)
            self.save()

    @property
    def url(self):
        return (self.settings.get("community_url") or DEFAULT_URL).rstrip("/")

    def request(self, method, path, body=None, timeout=15):
        """(data) from the server; CommunityError with its message otherwise."""
        h = {"User-Agent": UA, "X-Slop-Device": self.settings["device_secret"], "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        url = self.url + path
        try:
            if requests is not None:
                r = requests.request(method, url, data=data, headers=h, timeout=timeout)
                status, raw = r.status_code, r.content
            else:
                req = urllib.request.Request(url, data=data, headers=h, method=method)
                try:
                    resp = urllib.request.urlopen(req, timeout=timeout)
                    status, raw = resp.status, resp.read()
                except urllib.error.HTTPError as e:
                    status, raw = e.code, e.read()
        except Exception as e:
            raise CommunityError(503, "The community server can't be reached (%s)" % e.__class__.__name__)
        try:
            out = json.loads(raw.decode("utf-8"))
        except ValueError:
            out = {}
        if status >= 400:
            raise CommunityError(status, out.get("error") or "Community server: HTTP %d" % status)
        return out

    # ---------------------------------------------------------------- reading
    def ports(self, q="", sort="popular"):
        return self.request("GET", "/v1/ports?" + urllib.parse.urlencode({"q": q or "", "sort": sort or "popular"}))

    def port(self, repo):
        return self.request("GET", "/v1/ports/" + _repo_path(repo))

    def me(self):
        return self.request("GET", "/v1/me")

    def set_nick(self, nick):
        return self.request("PUT", "/v1/me", {"nick": nick})

    # ---------------------------------------------------------------- writing
    def share(self, r):
        cfg = {}
        for k in SHARED_FIELDS:
            v = r.get(k)
            if v not in (None, "", [], {}):
                cfg[k] = v
        return self.request("POST", "/v1/ports", {"repo": r["repo"], "title": r.get("title") or "",
                                                  "cover_url": r.get("cover_url") or "", "config": cfg})

    def unshare(self, repo):
        return self.request("DELETE", "/v1/ports/%s/share" % _repo_path(repo))

    def review(self, repo, rating, text, version=""):
        return self.request("POST", "/v1/ports/%s/reviews" % _repo_path(repo),
                            {"rating": int(rating), "text": text or "", "version": version or "", "device": device_name()})

    def unreview(self, repo):
        return self.request("DELETE", "/v1/ports/%s/reviews" % _repo_path(repo))

    def _send_install(self, repo, tag, details=True):
        """One install to the server; remembered when counted, so each version is sent once. details: the handheld's
        model and firmware with it (only when the owner said yes to counting)."""
        res = self.request("POST", "/v1/pulls", {"repo": repo, "tag": tag, "device": device_name() if details else "",
                                                 "cfw": cfw_name() if details else ""}, timeout=10)
        if res.get("counted"):
            self.settings.setdefault("counted_installs", {})[repo.lower()] = tag
            self.save()
        return bool(res.get("counted"))

    def count_install(self, repo, tag):
        """An install, counted in the background when the owner agreed; failures don't matter."""
        if self.settings.get("count_installs") is not True or not tag:
            return

        def send():
            try:
                self._send_install(repo, tag)
            except Exception:
                pass
        t = threading.Thread(target=send, name="count-install")
        t.daemon = True
        t.start()

    def count_shared(self, repo, tag):
        """Sharing a port says this handheld has it: its install counts, whatever the answer to counting was (the
        model and firmware only with a yes)."""
        if not tag or (self.settings.get("counted_installs") or {}).get(repo.lower()) == tag:
            return

        def send():
            try:
                self._send_install(repo, tag, details=self.settings.get("count_installs") is True)
            except Exception:
                pass
        t = threading.Thread(target=send, name="count-shared")
        t.daemon = True
        t.start()

    def sync_installs(self, installed):
        """The versions already on the handheld ([(repo, tag)]), counted once each when the owner agreed: installs
        made before the port was shared or before the yes count too. Only ports listed in the community are sent."""
        if self.settings.get("count_installs") is not True:
            return

        def run():
            with self._sync_lock:
                try:
                    done = self.settings.get("counted_installs") or {}
                    todo = [(r, t) for r, t in installed if t and done.get(r.lower()) != t]
                    if not todo:
                        return
                    listed = set(p["repo"].lower() for p in self.request("GET", "/v1/ports?limit=100&sort=name").get("ports", []))
                    n = 0
                    for repo, tag in todo:
                        if repo.lower() in listed and self._send_install(repo, tag):
                            n += 1
                    if n:
                        self.log("community: %d installed port%s counted" % (n, "" if n == 1 else "s"))
                except Exception:
                    pass
        t = threading.Thread(target=run, name="sync-installs")
        t.daemon = True
        t.start()


def _repo_path(repo):
    owner, _, name = (repo or "").partition("/")
    if not owner or not name or "/" in name:
        raise ValueError("A repo is owner/name")
    return urllib.parse.quote(owner, safe="") + "/" + urllib.parse.quote(name, safe="")
