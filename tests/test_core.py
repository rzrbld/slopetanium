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
        catalogue_tests(tmp)
        c.stop()
        print("OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
