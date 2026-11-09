#!/usr/bin/env python3
"""Large files end to end over the real HTTP server (opt-in, needs ~20 GB free and a minute or two):
  - a 5 GiB game file uploaded in chunks (as the page sends them), resumed after a wrong offset, checked chunk by chunk
  - a card that can't hold it (FAT32) refuses it before any byte is written
  - a whole-game backup with a 6 GiB file streamed as a download (ZIP64), checked, restored, checked again
  python3 tests/test_large.py [workdir]
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "port", "slopetanium", "app"))
import core  # noqa: E402
import web  # noqa: E402

GiB = 1024 ** 3
CHUNK = 64 * 1024 ** 2


def main():
    base = tempfile.mkdtemp(prefix="slop-large-", dir=sys.argv[1] if len(sys.argv) > 1 else None)
    try:
        ports = os.path.join(base, "ports")
        root = os.path.join(ports, "slopetanium")
        pdir = os.path.join(ports, "bigport")
        os.makedirs(root)
        os.makedirs(os.path.join(pdir, "source"))
        os.makedirs(os.path.join(pdir, "conf"))
        json.dump({"name": "bigport.zip", "items": ["Big.sh", "bigport/"], "attr": {"title": "Big"},
                   "status": {"md5": "x"}}, open(os.path.join(pdir, "port.json"), "w"))
        open(os.path.join(pdir, "conf", "save.dat"), "w").write("save")
        c = core.Core(root, "", catalogue=False)
        c.stop()
        r = c.add_repo({"repo": "someone/bigport", "port_name": "bigport.zip"})
        rid = r["id"]
        os.environ["SLOP_LOCAL_TOKEN"] = "t"
        srv = web.start(c, os.path.join(HERE, "..", "port", "slopetanium", "web"), 0, host="127.0.0.1")
        port = srv.server_address[1]

        def call(method, path, body=None):
            rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body, method=method,
                                        headers={"X-Local-Token": "t"})
            try:
                with urllib.request.urlopen(rq, timeout=600) as resp:
                    return resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())

        # 1. a 5 GiB upload, chunk by chunk; each chunk's bytes say where it belongs
        total = 5 * GiB + 12345
        q = "name=disc.iso&total=%d" % total
        code, st = call("GET", "/api/repos/%s/upload?%s" % (rid, q))
        assert code == 200 and st["have"] == 0, st
        t0 = time.time()
        off, n = 0, 0
        while off < total:
            ln = min(CHUNK, total - off)
            body = bytes([n % 251]) * ln
            if n == 3:   # a wrong offset first: the handheld says where to go on
                code, j = call("PUT", "/api/repos/%s/upload?%s&offset=%d" % (rid, q, 0), body)
                assert code == 409 and j["have"] == off, (code, j)
            code, j = call("PUT", "/api/repos/%s/upload?%s&offset=%d" % (rid, q, off), body)
            assert code == 200, (code, j)
            off, n = j["have"], n + 1
        secs = time.time() - t0
        assert j["done"], j
        path = os.path.join(pdir, "source", "disc.iso")
        assert os.path.getsize(path) == total
        with open(path, "rb") as fh:
            for k in range(n):
                fh.seek(k * CHUNK)
                assert fh.read(1)[0] == k % 251, "chunk %d out of place" % k
        print("upload: %.1f GiB in %.1f s (%.0f MB/s on loopback)" % (total / GiB, secs, total / secs / 1e6))

        # 2. a FAT32 card: refused before a byte is written
        real_fs = core.fs_info
        core.fs_info = lambda p: {"fs": "FAT32", "max_file": 4 * GiB - 1}
        try:
            code, st = call("GET", "/api/repos/%s/upload?name=other.iso&total=%d" % (rid, total))
            assert st["max_file"] == 4 * GiB - 1 and st["fs"] == "FAT32", st
            code, j = call("PUT", "/api/repos/%s/upload?name=other.iso&total=%d&offset=0" % (rid, total), b"x" * 10)
            assert code == 400 and "FAT32" in j["error"], (code, j)
            assert not os.path.exists(os.path.join(pdir, "source", "other.iso.part"))
        finally:
            core.fs_info = real_fs
        os.unlink(path)

        # 3. the whole game as a download: a 6 GiB data file (ZIP64), streamed; then restored
        os.makedirs(os.path.join(pdir, "data"))
        big = os.path.join(pdir, "data", "assets.bin")
        with open(big, "wb") as fh:
            blk = hashlib.sha256(b"slope").digest() * (1 << 15)   # 1 MiB of a known pattern
            for _ in range(6 * 1024):
                fh.write(blk)
        want = os.path.getsize(big)
        t0 = time.time()
        rq = urllib.request.Request("http://127.0.0.1:%d/api/repos/%s/backup-download?kind=full" % (port, rid),
                                    headers={"X-Local-Token": "t"})
        zpath = os.path.join(base, "bigport-full.zip")
        with urllib.request.urlopen(rq, timeout=3600) as resp, open(zpath, "wb") as out:
            assert resp.headers["Content-Type"] == "application/zip"
            assert "attachment" in resp.headers["Content-Disposition"]
            shutil.copyfileobj(resp, out, 1 << 20)
        secs = time.time() - t0
        with zipfile.ZipFile(zpath) as zf:
            info = zf.getinfo("data/assets.bin")
            assert info.file_size == want, info.file_size
            assert "conf/save.dat" in zf.namelist() and "source/" not in "".join(zf.namelist())
            assert zf.testzip() is None
        print("download: %.1f GiB zip in %.1f s (ZIP64: %s)" % (os.path.getsize(zpath) / GiB, secs, info.file_size > 0xFFFFFFFF))
        # restored over a port whose data is gone
        os.unlink(big)
        open(os.path.join(pdir, "conf", "save.dat"), "w").write("newer")
        imp = c.import_path(rid, "bigport-full.zip")
        shutil.move(zpath, imp)
        t0 = time.time()
        c.restore_backup(rid, imp)
        assert os.path.getsize(big) == want
        with open(big, "rb") as fh:
            fh.seek(want - (1 << 20))
            assert fh.read(1 << 20) == blk
        assert open(os.path.join(pdir, "conf", "save.dat")).read() == "save"
        print("restore: %.1f GiB in %.1f s" % (want / GiB, time.time() - t0))
        srv.shutdown()
        print("OK")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
