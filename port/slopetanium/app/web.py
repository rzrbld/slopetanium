# SPDX-License-Identifier: MIT
"""Small JSON API + single-page web UI, stdlib only (Python 3.7+)."""

import json
import os

import core as core_mod
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


def make_handler(core, web_dir):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Slopetanium"

        def log_message(self, fmt, *args):  # quiet
            pass

        # ------------------------------------------------------------ helpers
        def _send(self, status, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1 << 20:
                raise ApiError(413, "Request too large")
            if n == 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                raise ApiError(400, "Bad JSON")

        def _authed(self):
            pin = core.settings().get("pin")
            if self.headers.get("X-Pin") == pin:
                return True
            # the handheld's own UI (slopetanium-ui): localhost, with the launcher's token
            tok = os.environ.get("SLOP_LOCAL_TOKEN")
            return bool(tok) and self.client_address[0] == "127.0.0.1" and self.headers.get("X-Local-Token") == tok

        def _route(self, method):
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if method == "GET" and path in ("/", "/index.html"):
                with open(os.path.join(web_dir, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if method == "GET" and path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if not path.startswith("/api/"):
                raise ApiError(404, "Not found")
            if path == "/api/ping":
                return self._send(200, {"ok": True, "authed": self._authed()})
            if not self._authed():
                raise ApiError(401, "Enter the PIN shown on the handheld")

            parts = path.split("/")[2:]  # after /api/
            q = parse_qs(url.query)
            # game files from the browser: raw chunks, not JSON
            #   GET  /api/repos/<id>/upload?name=&total=[&single=1]  -> {have, done, free, saved_as}
            #   PUT  /api/repos/<id>/upload?name=&total=&offset=     body: the bytes from offset (at most 64 MB)
            if len(parts) == 3 and parts[0] == "repos" and parts[2] == "upload" and method in ("GET", "PUT"):
                name = (q.get("name") or [""])[0]
                total = int((q.get("total") or ["0"])[0])
                single = (q.get("single") or ["0"])[0] == "1"
                try:
                    core.get_repo(parts[1])
                except KeyError:
                    raise ApiError(404, "No such repo")
                if method == "GET":
                    return self._send(200, core.upload_status(parts[1], name, total, single))
                length = int(self.headers.get("Content-Length") or 0)
                if length > 64 << 20:
                    raise ApiError(413, "Chunk too large")
                offset = int((q.get("offset") or ["0"])[0])
                try:
                    res = core.upload_chunk(parts[1], name, offset, total, self.rfile.read, length, single)
                except core_mod.UploadOffset as e:
                    self.rfile.read(length)  # (drain it: the connection stays usable)
                    return self._send(409, {"error": "resume", "have": e.have})
                return self._send(200, res)
            body = self._body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}

            if parts == ["state"] and method == "GET":
                snap = core.snapshot()
                webc = getattr(core, "webc", None)
                snap["web"] = {"running": bool(webc and webc.running), "urls": webc.urls() if webc else [],
                               "pin": core.settings().get("pin"), "online": bool(webc and webc.local_ips())}
                return self._send(200, snap)
            if parts == ["web", "start"] and method == "POST":
                webc = getattr(core, "webc", None)
                if webc is None:
                    raise ApiError(404, "No web control")
                return self._send(200, {"urls": webc.start()})
            if parts == ["qr"] and method == "GET":
                try:
                    import qrcode
                except Exception:
                    raise ApiError(501, "qrcode library missing")
                qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_L)
                qr.add_data((q.get("text") or [""])[0])
                qr.make(fit=True)
                rows = ["".join("1" if v else "0" for v in row) for row in qr.get_matrix()]
                return self._send(200, {"rows": rows})
            if parts == ["settings"] and method in ("PUT", "PATCH"):
                core.update_settings(body)
                return self._send(200, {"ok": True})
            if parts == ["check"] and method == "POST":
                core.queue_check()
                return self._send(202, {"ok": True})
            if parts == ["update-all"] and method == "POST":
                jobs = core.queue_update_all()
                return self._send(202, {"queued": len(jobs)})
            if parts == ["seen"] and method == "POST":
                core.mark_seen(body.get("id"))
                return self._send(200, {"ok": True})
            if parts == ["catalogue", "add"] and method == "POST":
                r = core.add_from_catalogue(body.get("repo") or "")
                return self._send(201, {"id": r["id"]})
            if parts == ["gog", "link"] and method == "POST":
                try:
                    return self._send(200, core.gog_link(body.get("code") or ""))
                except Exception as e:
                    raise ApiError(400, str(e))
            if parts == ["gog", "unlink"] and method == "POST":
                core.gog_unlink()
                return self._send(200, {"ok": True})
            if parts == ["repos"]:
                if method == "GET":
                    return self._send(200, core.snapshot()["repos"])
                if method == "POST":
                    r = core.add_repo(body)
                    return self._send(201, {"id": r["id"]})
            if len(parts) >= 2 and parts[0] == "repos":
                rid = parts[1]
                try:
                    core.get_repo(rid)
                except KeyError:
                    raise ApiError(404, "No such repo")
                action = parts[2] if len(parts) > 2 else None
                if action is None:
                    if method == "GET":
                        for rv in core.snapshot()["repos"]:
                            if rv["id"] == rid:
                                return self._send(200, rv)
                    if method in ("PUT", "PATCH"):
                        core.update_repo(rid, body)
                        return self._send(200, {"ok": True})
                    if method == "DELETE":
                        core.delete_repo(rid, drop_cache=body.get("drop_cache", True))
                        return self._send(200, {"ok": True})
                if method == "POST":
                    if action == "check":
                        core.queue_check(rid)
                        return self._send(202, {"ok": True})
                    if action == "install":
                        job = core.queue_install(rid, body.get("tag"))
                        return self._send(202, {"job": job.id})
                    if action == "uninstall":
                        if body.get("confirm") != "uninstall":
                            raise ApiError(400, "Confirmation missing")
                        job = core.queue_uninstall(rid, backup=body.get("backup", True))
                        return self._send(202, {"job": job.id})
                    if action == "gog-download":
                        job = core.queue_gog(rid)
                        return self._send(202, {"job": job.id})
                    if action == "originals-delete":
                        if core.busy():
                            raise ApiError(409, "Wait for the running install to finish")
                        freed = core.delete_originals(rid, body.get("names"))
                        return self._send(200, {"freed": freed})
            raise ApiError(404 if method == "GET" else 405, "Unknown API call")

        def _handle(self, method):
            try:
                self._route(method)
            except ApiError as e:
                self._send(e.status, {"error": str(e)})
            except (ValueError, KeyError) as e:
                self._send(400, {"error": str(e).strip("'\"")})
            except Exception as e:  # pragma: no cover
                traceback.print_exc()
                self._send(500, {"error": "%s: %s" % (e.__class__.__name__, e)})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


def start(core, web_dir, port, host="0.0.0.0"):
    last = None
    for p in (port, port + 1, port + 2, 0):
        try:
            srv = _Server((host, p), make_handler(core, web_dir))
            break
        except OSError as e:
            last = e
    else:
        raise last
    t = threading.Thread(target=srv.serve_forever, name="web", daemon=True)
    t.start()
    return srv
