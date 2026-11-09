#!/usr/bin/env python3
"""Stand-in for PortMaster's harbourmaster CLI in host tests.

Mirrors what matters here: `install /abs/X.zip` extracts over the ports dir
(scripts too), writes <port>/port.json with name/items/files/status.md5;
`uninstall name` removes the port's items. Ports dir: $HM_PORTS_DIR.
"""
import hashlib, json, os, re, shutil, sys, zipfile

ports = os.environ["HM_PORTS_DIR"]
args = [a for a in sys.argv[1:] if not a.startswith("--")]

def clean(t):
    t = re.sub(r"[^a-zA-Z0-9 _\-\.]+", "", t.strip().lower())
    return re.sub(r"[ \.]+", ".", t)

if args[0] == "install":
    z = args[1]
    md5 = hashlib.md5(open(z, "rb").read()).hexdigest()
    with zipfile.ZipFile(z) as zf:
        pj = [n for n in zf.namelist() if n.endswith("/port.json") and n.count("/") == 1][0]
        info = json.loads(zf.read(pj))
        for i, n in enumerate(zf.infolist()):
            zf.extract(n, ports)
            print("Installing %d%%" % ((i + 1) * 100 // len(zf.infolist())))
    info["name"] = clean(os.path.basename(z))
    info["status"] = {"source": "file", "md5": md5, "status": "Installed"}
    info["files"] = {"port.json": pj}
    for it in info.get("items", []):
        info["files"][it] = it
    json.dump(info, open(os.path.join(ports, pj), "w"), indent=4)
    print("Port %r installed successfully." % info["attr"]["title"])
elif args[0] == "uninstall":
    for e in os.listdir(ports):
        p = os.path.join(ports, e, "port.json")
        if os.path.isfile(p):
            info = json.load(open(p))
            if info.get("name") == args[1]:
                for it in info.get("items", []):
                    q = os.path.join(ports, it.rstrip("/"))
                    print("- removing", it)
                    shutil.rmtree(q) if os.path.isdir(q) else os.unlink(q)
                print("Successfully uninstalled", args[1])
                sys.exit(0)
    print("Unknown port", args[1]); sys.exit(255)
