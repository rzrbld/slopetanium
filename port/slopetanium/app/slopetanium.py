#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Slopetanium: watch GitHub releases of PortMaster ports, update/roll back
through PortMaster's harbourmaster, from the handheld or any browser on the LAN.
"""

import argparse
import os
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def setup_paths(controlfolder):
    # PortMaster's vendored libraries: requests+certifi, qrcode, pysdl2.
    if controlfolder:
        for sub in ("exlibs", "pylibs"):
            p = os.path.join(controlfolder, sub)
            if os.path.isdir(p) and p not in sys.path:
                sys.path.append(p)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)


class WebControl(object):
    """The web interface, started on demand (the Connect tile) or at once (--headless)."""

    def __init__(self, core, web_mod, core_mod, web_dir, port):
        self.core, self.web_mod, self.core_mod = core, web_mod, core_mod
        self.web_dir, self.port = web_dir, port
        self.srv = None
        self.lock = threading.Lock()
        self._ips = (0, [])

    @property
    def running(self):
        return self.srv is not None

    def start(self):
        with self.lock:
            if self.srv is None:
                self.srv = self.web_mod.start(self.core, self.web_dir, self.port)
                self.core.log("web interface on port %d, PIN %s" % (self.srv.server_address[1],
                                                                      self.core.settings()["pin"]))
                for u in self.urls():
                    self.core.log("open %s/?pin=%s" % (u, self.core.settings()["pin"]))
        return self.urls()

    def local_ips(self):
        if time.time() - self._ips[0] > 5:
            self._ips = (time.time(), self.core_mod.local_ips())
        return self._ips[1]

    def urls(self):
        if not self.srv:
            return []
        return ["http://%s:%d" % (ip, self.srv.server_address[1]) for ip in self.local_ips()]

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controlfolder", default=os.environ.get("controlfolder", ""))
    ap.add_argument("--root", default=os.path.dirname(HERE), help="the port folder (slopetanium/)")
    ap.add_argument("--ports-dir", default=None)
    ap.add_argument("--harbourmaster", default=None, help="override (tests)")
    ap.add_argument("--port", type=int, default=None, help="web port")
    ap.add_argument("--headless", action="store_true", help="web interface only, no screen")
    ap.add_argument("--serve", action="store_true",
                    help="for the native UI: the API on 127.0.0.1 (port in conf/core.port), the LAN web interface on demand")
    args = ap.parse_args()

    setup_paths(args.controlfolder)
    import core as core_mod
    import web

    core = core_mod.Core(args.root, args.controlfolder, ports_dir=args.ports_dir,
                         harbourmaster=args.harbourmaster)
    port = args.port or core.settings().get("web_port", 8686)
    webc = WebControl(core, web, core_mod, os.path.join(args.root, "web"), port)
    core.webc = webc

    if args.serve:
        local = web.start(core, os.path.join(args.root, "web"), 0, host="127.0.0.1")
        pf = os.path.join(core.conf_dir, "core.port")
        with open(pf + ".tmp", "w") as fh:
            fh.write("%d\n" % local.server_address[1])
        os.replace(pf + ".tmp", pf)
        core.log("local API on 127.0.0.1:%d" % local.server_address[1])
        if core.settings().get("web_autostart"):
            webc.start()
        stopped = {"v": False}

        def on_term(*_a):
            stopped["v"] = True
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
        try:
            while not stopped["v"]:
                time.sleep(0.3)
            t0 = time.time()
            while core.busy() and time.time() - t0 < 600:  # never cut PortMaster off mid-install
                time.sleep(0.5)
        finally:
            try:
                os.unlink(pf)
            except OSError:
                pass
            core.stop()
            webc.stop()
            local.shutdown()
            core.save()
        return 0

    stop = {"v": False}

    def on_signal(*_a):
        stop["v"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    screen = None
    if not args.headless:
        try:
            import screen as screen_mod
            screen = screen_mod.Screen(core, webc, args.controlfolder, args.root)
        except Exception as e:
            core.log("no screen (%s); web interface only, stop with SIGTERM" % e)
            screen = None
    if screen is None or core.settings().get("web_autostart"):
        webc.start()

    try:
        if screen is not None:
            def watch():
                while not screen.quit:
                    if stop["v"]:
                        screen.quit = True
                    time.sleep(0.2)
            threading.Thread(target=watch, daemon=True).start()
            screen.run()
        else:
            while not stop["v"]:
                time.sleep(0.5)
        # Let a running install finish before leaving (never cut PortMaster off mid-extract).
        t0 = time.time()
        while core.busy() and time.time() - t0 < 600:
            time.sleep(0.5)
    finally:
        core.stop()
        webc.stop()
        if screen is not None:
            screen.close()
        core.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
