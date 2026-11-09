#!/usr/bin/env python3
"""Offline end-to-end test: fake GitHub + fake harbourmaster, real Core.

Runs: add repo, check, install old, update, keep user files, rollback from
cache, outside install detection, originals cleanup (incl. path escape),
uninstall with backup.  `python3 tests/test_core.py`
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "port", "slopetanium", "app"))
import core  # noqa: E402


def make_zip(path, version):
    pj = {"version": 4, "name": "demo.zip", "items": ["Demo.sh", "demo/"],
          "attr": {"title": "Demo Port", "porter": ["t"]}}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Demo.sh", "#!/bin/bash\necho %s\n" % version)
        zf.writestr("demo/port.json", json.dumps(pj))
        zf.writestr("demo/bin", "binary %s" % version)
        zf.writestr("demo/source/PUT_YOUR_GAME_FILES_HERE.txt", "put files here")


class FakeHttp(object):
    def __init__(self, zips):
        self.zips = zips  # tag -> path
        self.calls = 0
        self.downloads = 0

    def get_json(self, url, etag=None):
        self.calls += 1
        if etag == "e1":
            return 304, None, etag, "59"
        rels = []
        for i, tag in enumerate(["v3", "v2", "v1"]):
            rels.append({"tag_name": tag, "name": tag, "prerelease": tag == "v3", "draft": False,
                         "published_at": "2026-11-0%dT00:00:00Z" % (9 - i), "body": "notes " + tag,
                         "html_url": "https://example/" + tag,
                         "assets": [{"name": "demo.zip", "size": os.path.getsize(self.zips[tag]),
                                     "browser_download_url": tag, "digest": None},
                                    {"name": "demo-source.zip", "size": 1,
                                     "browser_download_url": "x"}]})
        return 200, rels, "e1", "59"

    def download(self, url, dest, progress=None, cancel=None):
        self.downloads += 1
        shutil.copy(self.zips[url], dest)
        return os.path.getsize(dest)


def wait(c):
    for _ in range(200):
        if not any(j.state in ("queued", "running") for j in c.jobs):
            return
        time.sleep(0.05)
    raise AssertionError("jobs did not finish")


class FakeGog(object):
    """GOG's side: a library with one game, its installer served from memory (a cut on the first try)."""

    def __init__(self, owned):
        self.owned_ids = set(owned)
        self.store = {}
        self.cuts = 1

    def linked(self):
        return bool(self.store.get("refresh_token"))

    def status(self):
        return {"linked": self.linked(), "user": self.store.get("user", ""), "login_url": "x"}

    def link(self, pasted):
        import gog
        code = gog.code_from(pasted)
        self.store.update(refresh_token="r-" + code, user="tester")
        return self.status()

    def unlink(self):
        self.store.clear()

    _owned = None

    def owned(self, max_age=600):
        self._owned = set(self.owned_ids)
        return self._owned

    def installers(self, game_id, language="English", system="windows"):
        return [{"url": "/downloads/demo/en1installer0", "name": "Demo", "size": "1 KB"}]

    def download(self, url, dest, progress=None):
        os.makedirs(dest, exist_ok=True)
        path = os.path.join(dest, "setup_demo_1.0.exe")
        open(path, "wb").write(b"x" * 1024)
        if progress:
            progress(1024, 1024)
        return path


def gog_tests(c, ports, tmp):
    import gog
    # the code from the landing page's address, or the bare code
    assert gog.code_from("https://embed.gog.com/on_login_success?origin=client&code=AbC123_-xyzABCDEFGHIJKLMNOP") \
        == "AbC123_-xyzABCDEFGHIJKLMNOP"
    try:
        gog.code_from("https://www.gog.com/")
        raise AssertionError("no code accepted")
    except gog.GogError:
        pass
    c.gog = FakeGog(owned=[42])
    r = c.state["repos"][0]
    c.update_repo(r["id"], {"gog": {"id": 42, "title": "Demo", "dest": "source", "files": "setup_demo_*"}})
    wait(c)
    c.queue_install(r["id"], "v2"); wait(c)       # installed again (uninstalled above)
    pdir = os.path.join(ports, "demo")
    assert not os.path.exists(os.path.join(pdir, "source", "setup_demo_1.0.exe")), "fetched while signed out"
    c.gog_link("https://embed.gog.com/on_login_success?origin=client&code=CODEcodeCODEcodeCODE12")
    v = c.repo_view(r)
    assert v["gog"]["owned"] is True and v["gog"]["present"] is False, v["gog"]
    # signed in: an install fetches the game files by itself
    c.queue_install(r["id"], "v1"); wait(c)
    assert os.path.isfile(os.path.join(pdir, "source", "setup_demo_1.0.exe")), [j.message for j in c.jobs[-3:]]
    assert c.repo_view(r)["gog"]["present"] is True
    # the token never leaves the core
    snap = json.dumps(c.snapshot())
    assert "r-CODE" not in snap and snap.count("refresh_token") == 0
    # a game the account doesn't own: a clear failure
    c.gog.owned_ids = set()
    c.queue_gog(r["id"]); wait(c)
    assert c.jobs[-1].state == "failed" and "not in your GOG library" in c.jobs[-1].message, c.jobs[-1].message


def upload_tests(c, ports):
    r = c.state["repos"][0]
    src = os.path.join(ports, "demo", "source")
    data = os.urandom(300000)
    feed = io.BytesIO(data)
    st = c.upload_status(r["id"], "game.iso", len(data))
    assert st["have"] == 0 and not st["done"] and st["free"] > 0
    c.upload_chunk(r["id"], "game.iso", 0, len(data), feed.read, 100000)
    # a chunk that doesn't continue the file: told where to resume
    try:
        c.upload_chunk(r["id"], "game.iso", 0, len(data), io.BytesIO(data).read, 100000)
        raise AssertionError("wrong offset accepted")
    except core.UploadOffset as e:
        assert e.have == 100000
    assert c.upload_status(r["id"], "game.iso", len(data))["have"] == 100000   # a new page resumes from here
    res = c.upload_chunk(r["id"], "game.iso", 100000, len(data), feed.read, 200000)
    assert res["done"] and open(os.path.join(src, "game.iso"), "rb").read() == data
    assert not os.path.exists(os.path.join(src, "game.iso.part"))
    assert c.upload_status(r["id"], "game.iso", len(data))["done"]
    # a folder upload keeps its paths; an empty file works
    c.upload_chunk(r["id"], "NFS/Drivers/dx7z.dll", 0, 3, io.BytesIO(b"abc").read, 3)
    c.upload_chunk(r["id"], "NFS/empty.txt", 0, 0, io.BytesIO(b"").read, 0)
    assert open(os.path.join(src, "NFS", "Drivers", "dx7z.dll"), "rb").read() == b"abc"
    assert os.path.isfile(os.path.join(src, "NFS", "empty.txt"))
    # nothing outside the game files folder
    for bad in ("../escape.bin", "/etc/passwd", "a/../../x", "..", ""):
        try:
            c.upload_status(r["id"], bad, 1)
            raise AssertionError("accepted %r" % bad)
        except ValueError:
            pass
    # the port's own name for a single upload (The ReThing: the_thing.iso)
    c.update_repo(r["id"], {"rename": {"*.iso": "the_thing.iso"}})
    c.upload_chunk(r["id"], "My Copy.ISO", 0, 2, io.BytesIO(b"hi").read, 2, single=True)
    assert open(os.path.join(src, "the_thing.iso"), "rb").read() == b"hi"
    # through the web interface: chunks over HTTP, the token's way in
    import web, urllib.request
    os.environ["SLOP_LOCAL_TOKEN"] = "tt"
    srv = web.start(c, os.path.join(HERE, "..", "port", "slopetanium", "web"), 0, host="127.0.0.1")
    port = srv.server_address[1]
    blob = os.urandom(5000)

    def req(method, q, body=None):
        rq = urllib.request.Request("http://127.0.0.1:%d/api/repos/%s/upload?%s" % (port, r["id"], q), data=body,
                                    method=method, headers={"X-Local-Token": "tt"})
        try:
            with urllib.request.urlopen(rq) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    import urllib.error
    assert req("PUT", "name=web.bin&total=5000&offset=0", blob[:3000]) == (200, {"have": 3000, "done": False})
    code, j = req("PUT", "name=web.bin&total=5000&offset=0", blob[:3000])
    assert code == 409 and j["have"] == 3000
    assert req("PUT", "name=web.bin&total=5000&offset=3000", blob[3000:])[1]["done"]
    assert open(os.path.join(src, "web.bin"), "rb").read() == blob
    assert req("GET", "name=../../x&total=1")[0] == 400
    srv.shutdown()
    # the originals now list the uploads (and The ReThing's rename)
    names = [f["name"] for f in c.originals(r)["files"]]
    assert "the_thing.iso" in names and "NFS" in names, names


def browser_tests(c, ports, tmp):
    """The file browser: one port's folder, nothing outside it."""
    import web, urllib.request, urllib.error, zipfile
    r = c.state["repos"][0]
    pdir = os.path.join(ports, "demo")
    os.makedirs(os.path.join(pdir, "conf"), exist_ok=True)
    with open(os.path.join(pdir, "conf", "game.ini"), "wb") as fh:
        fh.write(b"[video]\r\nwidth=640\r\n")
    with open(os.path.join(pdir, "conf", "save.bin"), "wb") as fh:
        fh.write(b"\0\1\2" * 10)
    secret = os.path.join(tmp, "secret.txt")
    open(secret, "w").write("nope")
    os.symlink(secret, os.path.join(pdir, "escape"))
    os.symlink(tmp, os.path.join(pdir, "escdir"))
    ls = c.fs_list(r["id"], "")
    by = dict((e["name"], e) for e in ls["entries"])
    assert by["conf"]["dir"] and by["escape"].get("outside") and by["escdir"].get("outside"), by
    conf = dict((e["name"], e) for e in c.fs_list(r["id"], "conf")["entries"])
    assert conf["game.ini"]["text"] and not conf["save.bin"]["text"]
    for bad in ("..", "../x", "conf/../../x", "escape", "escdir/secret.txt", "escdir"):
        try:
            c.fs_read(r["id"], bad) if bad != "escdir" else c.fs_list(r["id"], bad)
            raise AssertionError("allowed %r" % bad)
        except ValueError:
            pass
    # an edit keeps the CRLF endings; a change behind the editor's back is refused
    f = c.fs_read(r["id"], "conf/game.ini")
    assert f["crlf"] and f["text"] == "[video]\nwidth=640\n"
    c.fs_write(r["id"], "conf/game.ini", f["text"].replace("640", "1280"), f["mtime"], f["encoding"], f["crlf"])
    assert open(os.path.join(pdir, "conf", "game.ini"), "rb").read() == b"[video]\r\nwidth=1280\r\n"
    try:
        c.fs_write(r["id"], "conf/game.ini", "x", f["mtime"] - 100)
        raise AssertionError("stale save accepted")
    except core.FsConflict:
        pass
    try:
        c.fs_read(r["id"], "conf/save.bin")
        raise AssertionError("binary opened")
    except ValueError:
        pass
    # folders, renames, deletes; a link is deleted as itself, never what it points to
    c.fs_mkdir(r["id"], "", "mods")
    for bad in ("", "..", "a/b"):
        try:
            c.fs_mkdir(r["id"], "", bad)
            raise AssertionError("mkdir %r" % bad)
        except ValueError:
            pass
    c.fs_rename(r["id"], "mods", "mods2")
    assert os.path.isdir(os.path.join(pdir, "mods2"))
    c.fs_delete(r["id"], ["escape", "escdir"])
    assert os.path.exists(secret) and os.path.isdir(tmp)
    for bad in ([""], [".."], ["../demo"]):
        try:
            c.fs_delete(r["id"], bad)
            raise AssertionError("deleted %r" % bad)
        except ValueError:
            pass
    assert os.path.isdir(pdir)
    # over HTTP: an upload into a folder replaces a file of the same size; downloads of files and folders
    os.environ["SLOP_LOCAL_TOKEN"] = "tt"
    srv = web.start(c, os.path.join(HERE, "..", "port", "slopetanium", "web"), 0, host="127.0.0.1")
    port = srv.server_address[1]

    def req(method, path, body=None, raw=False):
        rq = urllib.request.Request("http://127.0.0.1:%d/api/repos/%s/%s" % (port, r["id"], path), data=body,
                                    method=method, headers={"X-Local-Token": "tt"})
        try:
            with urllib.request.urlopen(rq) as resp:
                d = resp.read()
                return resp.status, (d if raw else json.loads(d.decode()))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    new = b"\x09\x08\x07" * 10
    code, st = req("GET", "upload?name=save.bin&total=30&into=conf")
    assert code == 200 and not st["done"], st   # the same size there: still sent, to replace it
    assert req("PUT", "upload?name=save.bin&total=30&offset=0&into=conf", new)[1]["done"]
    assert open(os.path.join(pdir, "conf", "save.bin"), "rb").read() == new
    assert req("PUT", "upload?name=top.txt&total=2&offset=0&into=", b"hi")[1]["done"]
    assert open(os.path.join(pdir, "top.txt"), "rb").read() == b"hi"
    assert req("GET", "upload?name=x&total=1&into=..")[0] == 400
    assert req("GET", "fs/download?path=conf/save.bin", raw=True) == (200, new)
    code, z = req("GET", "fs/download?path=conf", raw=True)
    assert code == 200 and sorted(zipfile.ZipFile(io.BytesIO(z)).namelist()) == ["conf/game.ini", "conf/save.bin"]
    code, j = req("POST", "fs/write", json.dumps({"path": "conf/new.cfg", "text": "a=1\n"}).encode())
    assert code == 200 and open(os.path.join(pdir, "conf", "new.cfg")).read() == "a=1\n"
    code, j = req("POST", "fs/write", json.dumps({"path": "conf/new.cfg", "text": "b", "mtime": 1}).encode())
    assert code == 409, (code, j)
    assert req("POST", "fs/delete", json.dumps({"paths": ["conf/new.cfg", "top.txt", "mods2"]}).encode())[0] == 200
    assert req("GET", "fs?path=../..")[0] == 400
    srv.shutdown()
    shutil.rmtree(os.path.join(pdir, "conf"))


def community_tests(c):
    """The community client against a fake slopetanium.pw: the secret goes only there, installs count only on yes."""
    import threading, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self, body):
            n = int(self.headers.get("Content-Length") or 0)
            got = json.loads(self.rfile.read(n).decode()) if n else None
            seen.append((self.command, self.path, self.headers.get("X-Slop-Device"), got))
            data = json.dumps(body(got)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/v1/ports/"):
                return self._answer(lambda g: {"repo": "someone/Shared", "title": "Shared", "cover_url": "https://x/c.jpg",
                                               "config": {"originals_dir": "data", "evil": 1, "keep_on_uninstall": ["saves"]},
                                               "review_list": []})
            return self._answer(lambda g: {"ports": [{"repo": c.state["repos"][0]["repo"].upper(), "title": "x"}]})

        def do_POST(self):
            if self.path == "/v1/ports":
                return self._answer(lambda g: {"repo": g["repo"], "shared": True, "listed": True})
            return self._answer(lambda g: {"ok": True})

        do_DELETE = do_POST
        do_PUT = do_POST

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c.update_settings({"community_url": "http://127.0.0.1:%d" % srv.server_address[1]})
    secret = c.state["settings"]["device_secret"]
    assert len(secret) == 64 and "device_secret" not in json.dumps(c.snapshot())
    try:
        c.update_settings({"community_url": "http://evil.example"})
        raise AssertionError("plain http server accepted")
    except ValueError:
        pass
    r = c.state["repos"][0]
    r["originals_dir"] = "source"
    c.share_repo(r["id"], True)
    m, path, dev, body = seen[-1]
    assert (m, path, dev) == ("POST", "/v1/ports", secret) and body["config"]["originals_dir"] == "source" and r["shared"]
    assert "installed_tag" not in body["config"] and "history" not in body["config"]
    # install counts: not before the owner said yes
    n = len(seen)
    c.community.count_install(r["repo"], "v1")
    c.update_settings({"count_installs": False})
    c.community.count_install(r["repo"], "v1")
    time.sleep(0.3)
    assert len(seen) == n
    c.update_settings({"count_installs": True})
    c.community.count_install(r["repo"], "v1")
    for _ in range(50):
        if len(seen) > n:
            break
        time.sleep(0.05)
    assert seen[-1][1] == "/v1/pulls" and seen[-1][3]["tag"] == "v1"
    # the list knows what is watched here; adding a community port takes its sharer's settings (checked ones only)
    assert c.community_ports()["ports"][0]["watched"] == r["id"]
    nr = c.add_from_community("someone/Shared")
    assert nr["originals_dir"] == "data" and nr["keep_on_uninstall"] == ["saves"] and "evil" not in nr
    c.share_repo(r["id"], False)
    assert seen[-1][0] == "DELETE" and not r["shared"]
    c.delete_repo(nr["id"])
    r["originals_dir"] = "source"
    srv.shutdown()


def backup_tests(c, ports, tmp):
    import zipfile
    r = c.state["repos"][0]
    pdir = os.path.join(ports, "demo")
    os.makedirs(os.path.join(pdir, "conf"), exist_ok=True)
    open(os.path.join(pdir, "conf", "save1.dat"), "w").write("level 7")
    os.makedirs(os.path.join(pdir, "data"), exist_ok=True)
    open(os.path.join(pdir, "data", "big.bin"), "wb").write(b"G" * 5000)
    # saves, by hand and automatic (the last 5 kept)
    p = c.backup_saves(r, reason="test")
    assert p and zipfile.ZipFile(p).namelist() == ["slopetanium-backup.json", "conf/save1.dat"], zipfile.ZipFile(p).namelist()
    for i in range(7):
        c.backup_saves(r, auto=True)
        time.sleep(1.05) if i == 0 else None
    autos = [b for b in c.backups(r) if b["auto"]]
    assert 1 <= len(autos) <= 5, autos
    # the whole game streamed (as the browser downloads it): no originals, no leftovers
    open(os.path.join(pdir, "log.txt"), "w").write("log")
    open(os.path.join(pdir, "source", "half.iso.part"), "w").write("x")

    class Pipe(io.RawIOBase):     # forward only, like the HTTP response
        def __init__(self):
            self.buf = io.BytesIO()
        def write(self, b):
            return self.buf.write(b)
        def writable(self):
            return True
    import web
    pipe = Pipe()
    c.stream_backup(r["id"], "full", web._Unseekable(pipe))
    full = os.path.join(tmp, "game-full.zip")
    open(full, "wb").write(pipe.buf.getvalue())
    names = zipfile.ZipFile(full).namelist()
    assert "data/big.bin" in names and "conf/save1.dat" in names and "bin" in names, names
    assert not any(n.startswith("source/") for n in names) and "log.txt" not in names, names
    # restored over a changed port (as on another handheld): everything back, the newer saves backed up first
    open(os.path.join(pdir, "conf", "save1.dat"), "w").write("level 1")
    os.unlink(os.path.join(pdir, "data", "big.bin"))
    imp = c.import_path(r["id"], "game-full.zip")
    shutil.copy(full, imp)
    c.queue_restore(r["id"], imp); wait(c)
    assert c.jobs[-1].state == "done", c.jobs[-1].message
    assert open(os.path.join(pdir, "conf", "save1.dat")).read() == "level 7"
    assert os.path.getsize(os.path.join(pdir, "data", "big.bin")) == 5000
    # another port's backup, and a backup with a path out of the folder, are refused
    other = os.path.join(tmp, "other.zip")
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("slopetanium-backup.json", json.dumps({"slopetanium_backup": 1, "repo": "x/y"}))
    try:
        c.restore_backup(r["id"], other)
        raise AssertionError("another port's backup restored")
    except ValueError:
        pass
    evil = os.path.join(tmp, "evil.zip")
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("slopetanium-backup.json", json.dumps({"slopetanium_backup": 1, "repo": r["repo"]}))
        zf.writestr("../../escape.txt", "x")
    try:
        c.restore_backup(r["id"], evil)
        raise AssertionError("unsafe backup restored")
    except ValueError:
        pass
    assert not os.path.exists(os.path.join(ports, "..", "escape.txt"))
    # skip: the skipped release is not the update; an older one is never an "update"
    c.state["repos"][0]["prereleases"] = True
    c.check_repo(r["id"])
    c.queue_install(r["id"], "v3", force=True); wait(c)
    v = c.repo_view(r)
    assert v["installed_tag"] == "v3" and v["status"] == "current", v["status"]
    c.skip_release(r["id"], "v3")
    v = c.repo_view(r)
    assert v["latest_tag"] == "v2" and v["status"] == "current", (v["latest_tag"], v["status"])   # not "update" to v2
    c.queue_install(r["id"], "v2"); wait(c)
    assert c.repo_view(r)["status"] == "current" and not c.queue_update_all()
    c.skip_release(r["id"], "v3", False)
    assert c.repo_view(r)["status"] == "update"
    # safe install: low battery and no space stop it unless forced
    real_batt, real_free = core.battery, core.free_space
    try:
        core.battery = lambda: (12, False)
        try:
            c.queue_install(r["id"], "v3")
            raise AssertionError("installed on a low battery")
        except core.InstallBlocked as e:
            assert "Battery at 12%" in str(e)
        core.battery = lambda: (12, True)        # on the charger: fine
        core.free_space = lambda p: 10
        try:
            c.queue_install(r["id"], "v3")
            raise AssertionError("installed with no space")
        except core.InstallBlocked as e:
            assert "Not enough space" in str(e)
        core.free_space = real_free
        c.queue_install(r["id"], "v3"); wait(c)
        assert c.repo_view(r)["installed_tag"] == "v3"
    finally:
        core.battery, core.free_space = real_batt, real_free


def catalogue_tests(tmp):
    ports = os.path.join(tmp, "ports2")
    root = os.path.join(ports, "slopetanium")
    os.makedirs(root)
    c = core.Core(root, "", harbourmaster="/bin/false")
    c.http = FakeHttp({})
    c.stop()
    repos = [r["repo"] for r in c.state["repos"]]
    assert "rzrbld/ReRED_Faction_Duo" in repos and "rzrbld/slopetanium" in repos, repos
    rf2 = [r for r in c.state["repos"] if r["repo"] == "rzrbld/ReRED_Faction_Duo"][0]
    assert rf2["gog"]["id"] == 1207660613 and rf2["originals_dir"] == "source"
    assert rf2["buy"][0]["url"].startswith("https://www.gog.com/") and "setup_red_faction2" in rf2["hint"]
    thing = [r for r in c.state["repos"] if r["repo"] == "rzrbld/The-ReThing"][0]
    assert thing["rename"] == {"*.iso": "the_thing.iso"}
    # removed stays removed: a new start offers nothing again
    c.delete_repo(rf2["id"])
    c2 = core.Core(root, "", harbourmaster="/bin/false")
    c2.stop()
    assert "rzrbld/ReRED_Faction_Duo" not in [r["repo"] for r in c2.state["repos"]]
    # a hand-copied port (no port.json) under its alias folder counts as installed
    os.makedirs(os.path.join(ports, "vurus"))
    v = [r for r in c2.state["repos"] if r["repo"] == "rzrbld/vurus"][0]
    v["port_name"] = "vurus-portmaster.zip"
    assert c2.port_dir(v) == os.path.join(ports, "vurus")
    # originals by pattern in the port folder itself: only the matching files
    open(os.path.join(ports, "vurus", "game.iso"), "w").write("i")
    open(os.path.join(ports, "vurus", "vurus.aarch64"), "w").write("b")
    assert [f["name"] for f in c2.originals(v)["files"]] == ["game.iso"]


def main():
    tmp = tempfile.mkdtemp(prefix="slop-test-")
    try:
        ports = os.path.join(tmp, "ports")
        root = os.path.join(ports, "slopetanium")
        os.makedirs(root)
        os.environ["HM_PORTS_DIR"] = ports
        zips = {}
        for tag in ("v1", "v2", "v3"):
            zips[tag] = os.path.join(tmp, "%s.zip" % tag)
            make_zip(zips[tag], tag)
        c = core.Core(root, "", harbourmaster=os.path.join(HERE, "fake_harbourmaster.py"), catalogue=False)
        c.http = FakeHttp(zips)

        r = c.add_repo({"repo": "https://github.com/someone/demo-port", "prereleases": False})
        wait(c)
        v = c.repo_view(r)
        assert v["latest_tag"] == "v2", v["latest_tag"]          # v3 is a pre-release
        assert v["status"] == "not_installed"
        assert r["port_name"] == "demo.zip"
        assert c.releases(r)[0]["asset"]["name"] == "demo.zip"   # not the source zip

        c.queue_install(r["id"], "v1"); wait(c)
        assert c.repo_view(r)["status"] == "update", c.repo_view(r)
        pdir = os.path.join(ports, "demo")
        os.makedirs(os.path.join(pdir, "data")); open(os.path.join(pdir, "data", "game.dat"), "w").write("x")
        os.makedirs(os.path.join(pdir, "conf")); open(os.path.join(pdir, "conf", "save"), "w").write("s")
        open(os.path.join(pdir, "source", "setup.exe"), "w").write("y" * 1000)

        # A running port is never updated under its feet.
        import subprocess
        open(os.path.join(pdir, "busy.sh"), "w").write("sleep 30\n")
        busy = subprocess.Popen(["/bin/sh", os.path.join(pdir, "busy.sh")])
        time.sleep(0.2)
        try:
            c.queue_update_all(); wait(c)
            assert c.jobs[-1].state == "failed" and "running" in c.jobs[-1].message, c.jobs[-1].message
            assert c.repo_view(r)["installed_tag"] == "v1"
        finally:
            busy.kill(); busy.wait()

        assert len(c.queue_update_all()) == 1; wait(c)
        v = c.repo_view(r)
        assert v["installed_tag"] == "v2" and v["status"] == "current", v
        assert open(os.path.join(pdir, "bin")).read() == "binary v2"
        assert os.path.isfile(os.path.join(pdir, "data", "game.dat")), "user data lost on update"
        assert v["previous_tag"] == "v1"

        d0 = c.http.downloads
        c.queue_install(r["id"], "v1"); wait(c)
        assert c.http.downloads == d0, "rollback should come from the cache"
        assert c.repo_view(r)["installed_tag"] == "v1"

        # Installed outside Slopetanium (e.g. PortMaster) with a cached zip -> recognised.
        import subprocess
        cached_v2 = os.path.join(c._cache_dir(r), "v2", "demo.zip")
        subprocess.check_call([sys.executable, os.path.join(HERE, "fake_harbourmaster.py"),
                               "install", cached_v2], stdout=subprocess.DEVNULL)
        assert c.snapshot()["repos"][0]["installed_tag"] == "v2"

        o = c.originals(r)
        assert [f["name"] for f in o["files"]] == ["setup.exe"], o
        assert o["imported_hint"] == "data"
        assert c.delete_originals(r["id"], ["../../etc", "setup.exe"]) == 1000
        assert os.listdir(os.path.join(pdir, "source")) == ["PUT_YOUR_GAME_FILES_HERE.txt"]

        c.queue_uninstall(r["id"]); wait(c)
        assert not os.path.exists(pdir) and not os.path.exists(os.path.join(ports, "Demo.sh"))
        backups = os.listdir(c.backup_dir)
        backups = [b for b in backups if b.startswith("demo-")]   # (demo/: the automatic saves before updates)
        assert len(backups) == 1 and os.path.isfile(os.path.join(c.backup_dir, backups[0], "conf", "save"))
        assert c.repo_view(r)["status"] == "not_installed"

        # Conditional request: second check answers 304 and keeps releases.
        c.check_repo(r["id"])
        assert c.repo_view(r)["latest_tag"] == "v2"

        try:
            core.parse_repo("not a repo")
            raise AssertionError("bad repo accepted")
        except ValueError:
            pass
        assert core.parse_repo("https://github.com/a/b.git") == "a/b"
        gog_tests(c, ports, tmp)
        upload_tests(c, ports)
        browser_tests(c, ports, tmp)
        community_tests(c)
        backup_tests(c, ports, tmp)
        catalogue_tests(tmp)
        c.stop()
        print("OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
