# SPDX-License-Identifier: MIT
"""Install self-contained Linux app ZIPs from their original release assets.

No app sources, repackaged releases or per-project forks live here. The archive
stays intact in Slopetanium's release cache; only a launcher and ownership record
are generated locally. PortMaster ports continue to use harbourmaster.
"""
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import tempfile
import zipfile

MARKER = ".slopetanium-app.json"


def digest(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def clean_path(name):
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise ValueError("Unsafe app archive path: %r" % name)
    bits = name.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in bits) or ":" in bits[0]:
        raise ValueError("Unsafe app archive path: %r" % name)
    if bits[0] == MARKER:
        raise ValueError("App archive contains Slopetanium's ownership record")
    return "/".join(bits)


def inspect(path, entry=""):
    """Validate the whole archive and find a launcher without executing it."""
    files = {}
    with zipfile.ZipFile(path) as z:
        for item in z.infolist():
            name = clean_path(item.filename)
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("Links and special files are not supported in app ZIPs: " + name)
            if item.flag_bits & 1:
                raise ValueError("Encrypted app ZIPs are not supported")
            if not item.is_dir():
                if name in files:
                    raise ValueError("Duplicate app archive path: " + name)
                files[name] = item
        # Reject a file also used as a directory, including duplicate prefixes.
        for name in files:
            parts = name.split("/")
            if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
                raise ValueError("Conflicting app archive paths: " + name)
        bad = z.testzip()
        if bad:
            raise ValueError("Corrupt app archive member: " + bad)
    if entry:
        entry = clean_path(entry)
        if entry not in files or not entry.lower().endswith(".sh"):
            raise ValueError("App launcher must name a .sh file in the release ZIP")
    else:
        choices = [n for n in files if "/" not in n and n.lower().endswith(".sh")]
        if not choices:
            choices = [n for n in files if n.count("/") <= 1 and os.path.basename(n) in ("launch.sh", "run.sh")]
        if len(choices) != 1:
            raise ValueError("App ZIP needs one launch script, or choose its App launcher in Edit")
        entry = choices[0]
    return {"entry": entry, "files": files, "size": sum(i.file_size for i in files.values()),
            "largest": max([i.file_size for i in files.values()] or [0])}


def compatible_asset(name, machine=None):
    machine = (machine or os.environ.get("DEVICE_ARCH") or platform.machine()).lower()
    aliases = {"aarch64": ("aarch64", "arm64"), "arm64": ("aarch64", "arm64"),
               "x86_64": ("x86_64", "amd64"), "amd64": ("x86_64", "amd64"),
               "armv7l": ("armv7", "armv7l", "armhf"), "armhf": ("armv7", "armv7l", "armhf"),
               "i686": ("i386", "i686", "x86")}
    known = set(v for values in aliases.values() for v in values)
    named = [v for v in known if re.search(r"(?:^|[-_.])" + v + r"(?:[-_.]|$)", name, re.I)]
    return not named or any(v in aliases.get(machine, (machine,)) for v in named)


def is_config(name):
    bits = name.lower().split("/")
    return any(p in ("conf", "config", "settings", "saves", "states") for p in bits[:-1]) or \
        name.lower().endswith((".cfg", ".ini", ".conf"))


class Apps(object):
    def __init__(self, root, shortcuts, target_arch=None):
        self.target_arch = target_arch
        self.root = os.path.realpath(root)
        self.shortcuts = os.path.realpath(shortcuts)

    def folder(self, repo):
        # repo has already been parsed by Core, but this module is safe standalone.
        if not re.match(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z", repo):
            raise ValueError("Invalid app repository")
        return repo.lower().replace("/", "__")

    def info(self, repo):
        path = os.path.join(self.root, self.folder(repo))
        if os.path.islink(path):
            raise ValueError("App installation directory is a symlink")
        try:
            marker = os.path.join(path, MARKER)
            if os.path.islink(marker):
                raise ValueError("App ownership record is a symlink")
            with open(marker) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("repo"), str) or data["repo"].lower() != repo.lower():
            return None
        shortcut = data.get("shortcut", "")
        if not all(isinstance(data.get(k), str) and data[k] for k in ("title", "tag", "md5", "entry")):
            return None
        if not shortcut or os.path.basename(shortcut) != shortcut or not isinstance(data.get("files"), dict):
            return None
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in data["files"].items()):
            return None
        return {"_dir": path, "_dirname": os.path.basename(path), "attr": {"title": data["title"]},
                "status": {"md5": data["md5"]}, "items": [os.path.join(self.shortcuts, data["shortcut"])],
                "_app": data, "name": data["shortcut"]}

    def _wrapper(self, repo, entry):
        path = os.path.join(self.root, self.folder(repo), entry)
        return "#!/bin/bash\n# Slopetanium app: %s\nexec bash %s \"$@\"\n" % (repo, shlex.quote(path))

    def install(self, repo, tag, archive, entry="", check_fits=None):
        if not compatible_asset(os.path.basename(archive), self.target_arch):
            raise ValueError("This app release is built for another CPU architecture")
        plan = inspect(archive, entry)
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.shortcuts, exist_ok=True)
        folder = self.folder(repo)
        target = os.path.join(self.root, folder)
        previous = target + ".previous"
        old = self.info(repo)
        # Recover a process stopped between the two atomic directory renames.
        if not os.path.lexists(target) and os.path.isdir(previous) and not os.path.islink(previous):
            os.rename(previous, target)
            old = self.info(repo)
        if os.path.lexists(target) and not old:
            raise ValueError("Refusing to replace an app directory Slopetanium does not own")
        if os.path.lexists(previous):
            # Only our own previous installation can be cleaned up.
            if os.path.islink(previous) or os.path.islink(os.path.join(previous, MARKER)):
                raise ValueError("Previous app directory is a symlink")
            with open(os.path.join(previous, MARKER)) as f:
                if json.load(f).get("repo", "").lower() != repo.lower():
                    raise ValueError("Unowned previous app directory")
            if os.path.islink(previous):
                raise ValueError("Previous app directory is a symlink")
            shutil.rmtree(previous)
        title = os.path.splitext(os.path.basename(plan["entry"]))[0]
        shortcut = (old or {}).get("_app", {}).get("shortcut") or title + ".sh"
        if os.path.basename(shortcut) != shortcut:
            raise ValueError("Invalid app shortcut")
        shortcut_path = os.path.join(self.shortcuts, shortcut)
        wrapper = self._wrapper(repo, plan["entry"])
        if os.path.lexists(shortcut_path):
            with open(shortcut_path) as f:
                owned = "# Slopetanium app: %s\n" % repo in f.read()
            if os.path.islink(shortcut_path) or not owned:
                raise ValueError("Another app already uses the shortcut " + shortcut)
        if check_fits:
            check_fits(self.root, plan["largest"], "A file in this app release")
        needed = plan["size"]
        if old:
            needed += sum(os.lstat(os.path.join(d, n)).st_size for d, _, ns in os.walk(target) for n in ns)
        if shutil.disk_usage(self.root).free < needed:
            raise RuntimeError("Not enough space to stage this app update")
        stage = tempfile.mkdtemp(prefix=".slop-app-", dir=self.root)
        temp_link = None
        moved = False
        published = False
        try:
            old_files = ((old or {}).get("_app") or {}).get("files", {})
            if old:
                # Keep saves and other user-created files while replacing code.
                shutil.rmtree(stage)
                shutil.copytree(target, stage, symlinks=True)
            hashes = {}
            with zipfile.ZipFile(archive) as z:
                for name, item in plan["files"].items():
                    dest = os.path.join(stage, name)
                    parent = os.path.dirname(dest)
                    if os.path.commonpath([os.path.realpath(parent), stage]) != stage or os.path.islink(dest):
                        raise ValueError("App archive would write through an existing link: " + name)
                    os.makedirs(parent, exist_ok=True)
                    keep = old and is_config(name) and os.path.isfile(dest) and \
                        (name not in old_files or digest(dest) != old_files[name])
                    h = hashlib.sha256()
                    prefix = b""
                    output = None if keep else open(dest, "wb")
                    try:
                        with z.open(item) as source:
                            while True:
                                chunk = source.read(1 << 20)
                                if not chunk:
                                    break
                                if not prefix:
                                    prefix = chunk[:4]
                                h.update(chunk)
                                if output:
                                    output.write(chunk)
                    finally:
                        if output:
                            output.close()
                    hashes[name] = h.hexdigest()
                    if not keep:
                        executable = bool((item.external_attr >> 16) & 0o111) or \
                            name.lower().endswith(".sh") or prefix == b"\x7fELF"
                        os.chmod(dest, 0o755 if executable else 0o644)
            for name, sha in old_files.items():
                if name not in hashes:
                    path = os.path.join(stage, clean_path(name))
                    if os.path.commonpath([os.path.realpath(path), stage]) != stage:
                        raise ValueError("Old app path escapes its directory")
                    if os.path.isfile(path) and not os.path.islink(path) and digest(path) == sha:
                        os.unlink(path)
            data = {"repo": repo, "tag": tag, "title": title, "entry": plan["entry"],
                    "shortcut": shortcut, "md5": digest(archive, "md5"), "files": hashes}
            with open(os.path.join(stage, MARKER), "w") as f:
                json.dump(data, f, indent=2)
            fd, temp_link = tempfile.mkstemp(prefix=".slop-launch-", dir=self.shortcuts)
            with os.fdopen(fd, "w") as f:
                f.write(wrapper)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_link, 0o755)
            if old:
                os.rename(target, previous)
                moved = True
            os.rename(stage, target)
            published = True
            os.replace(temp_link, shortcut_path)
            temp_link = None
            if moved:
                shutil.rmtree(previous, ignore_errors=True)
            return self.info(repo)
        except Exception:
            if published:
                shutil.rmtree(target)
            if moved:
                os.rename(previous, target)
            raise
        finally:
            if os.path.isdir(stage):
                shutil.rmtree(stage)
            if temp_link and os.path.exists(temp_link):
                os.unlink(temp_link)

    def uninstall(self, repo):
        info = self.info(repo)
        if not info:
            raise ValueError("App is not installed by Slopetanium")
        shortcut = info["items"][0]
        if os.path.isfile(shortcut) and not os.path.islink(shortcut):
            with open(shortcut) as f:
                if "# Slopetanium app: %s\n" % repo in f.read():
                    os.unlink(shortcut)
        shutil.rmtree(info["_dir"])
