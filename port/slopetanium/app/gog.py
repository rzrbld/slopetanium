# SPDX-License-Identifier: MIT
"""GOG.com: the user's own account, to fetch the original game files a port needs.

The sign-in is GOG Galaxy's own OAuth flow, as lgogdownloader, Heroic and Minigalaxy use it: the user signs in on
gog.com in a browser, lands on a page whose address carries a one-time code, and pastes that address (or just the
code) into Slopetanium's web interface. Only the refresh token is kept (conf/state.json); the password never passes
through Slopetanium.

Downloads are the game's own installers from the user's library (account/gameDetails), resumed when cut off.
Python 3.7 compatible.
"""

import json
import os
import re
import time
import urllib.parse

try:
    import requests  # PortMaster's exlibs
except Exception:  # pragma: no cover
    requests = None

import urllib.request
import urllib.error

# GOG Galaxy's public client (the same id and secret every open-source GOG client uses)
CLIENT_ID = "46899977096215655"
CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
REDIRECT = "https://embed.gog.com/on_login_success?origin=client"
AUTH = "https://auth.gog.com"
EMBED = "https://embed.gog.com"
UA = "slopetanium/0.1 (GOG downloads of the user's own games)"

LOGIN_URL = "%s/auth?client_id=%s&redirect_uri=%s&response_type=code&layout=client2" % (
    AUTH, CLIENT_ID, urllib.parse.quote(REDIRECT, safe=""))


class GogError(Exception):
    pass


def code_from(text):
    """The code from what the user pasted: the landing page's address, or the code itself."""
    text = (text or "").strip()
    m = re.search(r"[?&]code=([A-Za-z0-9_\-]+)", text)
    if m:
        return m.group(1)
    if re.match(r"^[A-Za-z0-9_\-]{20,}$", text):
        return text
    raise GogError("That is not the address GOG sent you to (it has ?code=... in it)")


def _get(url, headers=None, timeout=30):
    """(status, body bytes, final url)"""
    h = {"User-Agent": UA}
    h.update(headers or {})
    if requests is not None:
        r = requests.get(url, headers=h, timeout=timeout)
        return r.status_code, r.content, r.url
    req = urllib.request.Request(url, headers=h)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read(), resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read(), url


class Gog(object):
    def __init__(self, store, save, log):
        """store: the dict kept in state.json (settings["gog"]); save(): persist it; log(msg)."""
        self.store = store
        self.save = save
        self.log = log
        self._access = None
        self._access_until = 0
        self._owned = None
        self._owned_at = 0

    # ------------------------------------------------------------ account
    def linked(self):
        return bool(self.store.get("refresh_token"))

    def status(self):
        return {"linked": self.linked(), "user": self.store.get("user") or "", "login_url": LOGIN_URL}

    def _token(self, params):
        q = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        q.update(params)
        st, body, _ = _get("%s/token?%s" % (AUTH, urllib.parse.urlencode(q)))
        try:
            d = json.loads(body.decode("utf-8"))
        except ValueError:
            d = {}
        if st != 200 or "access_token" not in d:
            raise GogError(d.get("error_description") or d.get("error") or "GOG sign-in failed (HTTP %d)" % st)
        self._access = d["access_token"]
        self._access_until = time.time() + int(d.get("expires_in", 3600)) - 120
        if d.get("refresh_token"):
            self.store["refresh_token"] = d["refresh_token"]
        if d.get("user_id"):
            self.store["user_id"] = str(d["user_id"])
        return d

    def link(self, pasted):
        self._token({"grant_type": "authorization_code", "code": code_from(pasted), "redirect_uri": REDIRECT})
        try:
            st, body, _ = _get(EMBED + "/userData.json", self._auth())
            if st == 200:
                self.store["user"] = json.loads(body.decode("utf-8")).get("username") or ""
        except Exception:
            pass
        self._owned = None
        self.save()
        self.log("GOG: signed in as %s" % (self.store.get("user") or "?"))
        return self.status()

    def unlink(self):
        for k in ("refresh_token", "user", "user_id"):
            self.store.pop(k, None)
        self._access = None
        self._owned = None
        self.save()
        self.log("GOG: signed out")

    def _auth(self):
        if not self.linked():
            raise GogError("Not signed in to GOG")
        if not self._access or time.time() > self._access_until:
            self._token({"grant_type": "refresh_token", "refresh_token": self.store["refresh_token"]})
            self.save()
        return {"Authorization": "Bearer " + self._access}

    # ------------------------------------------------------------ library
    def owned(self, max_age=600):
        """The ids of the games the account owns (cached a few minutes)."""
        if self._owned is not None and time.time() - self._owned_at < max_age:
            return self._owned
        st, body, _ = _get(EMBED + "/user/data/games", self._auth())
        if st != 200:
            raise GogError("GOG library: HTTP %d" % st)
        self._owned = set(int(i) for i in json.loads(body.decode("utf-8")).get("owned", []))
        self._owned_at = time.time()
        return self._owned

    def installers(self, game_id, language="English", system="windows"):
        """The installer parts of a game: [{"url": manualUrl, "name", "size" (text), "version"}]."""
        st, body, _ = _get("%s/account/gameDetails/%d.json" % (EMBED, int(game_id)), self._auth())
        if st != 200:
            raise GogError("GOG game details: HTTP %d" % st)
        d = json.loads(body.decode("utf-8"))
        langs = d.get("downloads") or []
        pick = None
        for lang, by_os in langs:
            if lang.lower() == language.lower():
                pick = by_os
        if pick is None and langs:
            pick = langs[0][1]
        out = []
        for f in (pick or {}).get(system, []):
            out.append({"url": f.get("manualUrl"), "name": f.get("name"), "size": f.get("size"),
                        "version": f.get("version")})
        return out

    # ------------------------------------------------------------ download
    def download(self, manual_url, dest_dir, progress=None, attempts=4):
        """One installer part into dest_dir under GOG's own file name; resumes a .part left by a cut. Returns the
        file's path."""
        if requests is None:
            raise GogError("GOG downloads need PortMaster's requests library")
        url = EMBED + manual_url
        delay = 3
        last = None
        for i in range(attempts):
            try:
                return self._download_once(url, dest_dir, progress)
            except (GogError, IOError, OSError) as e:
                last = e
                if i == attempts - 1:
                    break
                if progress:
                    progress(0, 0, "retry %d/%d after: %s" % (i + 2, attempts, e))
                time.sleep(delay)
                delay *= 2
        raise GogError(str(last))

    def _download_once(self, url, dest_dir, progress):
        # the library link answers with a redirect to the CDN; the last part of its path is the file name
        h = {"User-Agent": UA}
        h.update(self._auth())
        r = requests.get(url, headers=h, allow_redirects=False, timeout=30)
        hops = 0
        while r.status_code in (301, 302, 303, 307, 308) and hops < 5:
            url = urllib.parse.urljoin(url, r.headers["Location"])
            r = requests.get(url, headers={"User-Agent": UA}, allow_redirects=False, timeout=30, stream=True)
            hops += 1
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
        if r.status_code != 200 or not name or name in (".", ".."):
            raise GogError("GOG download: HTTP %d" % r.status_code)
        total = int(r.headers.get("Content-Length") or 0)
        r.close()
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir)
        final = os.path.join(dest_dir, name)
        if os.path.isfile(final) and total and os.path.getsize(final) == total:
            return final  # already there
        part = final + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if have > total:
            have = 0
        headers = {"User-Agent": UA}
        if have:
            headers["Range"] = "bytes=%d-" % have
        with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
            if resp.status_code == 200:
                have = 0  # the server sends it whole
            elif resp.status_code != 206:
                raise GogError("GOG download: HTTP %d" % resp.status_code)
            done = have
            with open(part, "ab" if have else "wb") as fh:
                for chunk in resp.iter_content(1 << 17):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if total and os.path.getsize(part) != total:
            raise GogError("GOG download cut off at %d of %d bytes" % (os.path.getsize(part), total))
        os.replace(part, final)
        return final
