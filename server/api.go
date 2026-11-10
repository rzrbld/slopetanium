// SPDX-License-Identifier: MIT
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"
)

type Server struct {
	db       *sql.DB
	salt     []byte
	gh       *GitHub
	igdb     *IGDB    // nil without credentials
	writes   *limiter // per IP address (kept in memory only, never written down)
	calls    *limiter // any call carrying a device id, per network
	reviews  *limiter // reviews a day, per network
	newPorts *limiter // ports new to the community a day, per network
	byAuth   *limiter // per author
	pulls    *limiter
	site     *Site
}

// ------------------------------------------------------------------ helpers

type apiError struct {
	status int
	msg    string
}

func (e apiError) Error() string { return e.msg }

func fail(status int, msg string) error { return apiError{status, msg} }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) api(h func(w http.ResponseWriter, r *http.Request) error) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*") // read-only data, no cookies
		if err := h(w, r); err != nil {
			var ae apiError
			if errors.As(err, &ae) {
				writeJSON(w, ae.status, map[string]string{"error": ae.msg})
				return
			}
			logf("api %s %s: %v", r.Method, r.URL.Path, err)
			writeJSON(w, 500, map[string]string{"error": "Server error"})
		}
	}
}

func readBody(r *http.Request, v any) error {
	r.Body = http.MaxBytesReader(nil, r.Body, 32<<10)
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(v); err != nil {
		return fail(400, "Bad JSON")
	}
	return nil
}

// clientIP: who is asking, for rate limits only (never stored). Behind Caddy (the server listens on localhost
// only) it's the X-Real-IP Caddy sets from the connection. An IPv6 address counts by its /64: one network, one key.
func clientIP(r *http.Request) string {
	h := strings.TrimSpace(r.Header.Get("X-Real-IP"))
	if h == "" {
		h, _, _ = net.SplitHostPort(r.RemoteAddr)
	}
	ip := net.ParseIP(h)
	if ip == nil {
		return h
	}
	if ip.To4() == nil {
		return ip.Mask(net.CIDRMask(64, 128)).String() + "/64"
	}
	return ip.String()
}

var secretRe = regexp.MustCompile(`^[0-9a-f]{32,128}$`)

// author: who is writing, from the handheld's random secret. Only a keyed hash of it is kept, so the secret can't be
// recovered from the database, and nobody without the salt can link a review to a handheld.
// Nothing is written here: a handheld gets a row only when it shares, reviews, names itself or counts an install
// (after that call's rate limit), so made-up ids cost the server nothing.
func (s *Server) author(r *http.Request) (string, error) {
	if !s.calls.allow(clientIP(r)) {
		return "", fail(429, "Too many requests; try again in a minute")
	}
	sec := strings.ToLower(strings.TrimSpace(r.Header.Get("X-Slop-Device")))
	if !secretRe.MatchString(sec) {
		return "", fail(401, "Slopetanium sends a device id with this call")
	}
	m := hmac.New(sha256.New, s.salt)
	m.Write([]byte(sec))
	id := hex.EncodeToString(m.Sum(nil))[:24]
	var banned int
	err := s.db.QueryRow(`SELECT banned FROM authors WHERE id = ?`, id).Scan(&banned)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return "", err
	}
	if banned != 0 {
		return "", fail(403, "This handheld can't post to the community")
	}
	return id, nil
}

// touch: the author's row, when it writes something
func (s *Server) touch(author string) error {
	t := now()
	_, err := s.db.Exec(`INSERT INTO authors (id, created, last_seen) VALUES (?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen`, author, t, t)
	return err
}

func (s *Server) limitWrite(r *http.Request, author string, extra ...*limiter) error {
	if !s.writes.allow(clientIP(r)) || !s.byAuth.allow(author) {
		return fail(429, "Too many changes at once; try again in a few minutes")
	}
	for _, l := range extra {
		if !l.allow(clientIP(r)) {
			return fail(429, "That's a lot for one day; try again tomorrow")
		}
	}
	return s.touch(author)
}

// cleanText: printable text, trimmed, at most n characters (newlines kept when multiline)
func cleanText(v string, n int, multiline bool) string {
	var b strings.Builder
	count := 0
	for _, r := range strings.TrimSpace(v) {
		if r == '\r' {
			continue
		}
		if r == '\n' || r == '\t' {
			if !multiline {
				r = ' '
			}
		} else if !unicode.IsPrint(r) {
			continue
		}
		if count >= n {
			break
		}
		b.WriteRune(r)
		count++
	}
	out := b.String()
	if multiline {
		for strings.Contains(out, "\n\n\n") {
			out = strings.ReplaceAll(out, "\n\n\n", "\n\n")
		}
	}
	return strings.TrimSpace(out)
}

var repoRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$`)

// repoFrom: owner/name from what the user typed (a github.com link works too)
func repoFrom(v string) (string, error) {
	v = strings.TrimSpace(v)
	v = strings.TrimPrefix(strings.TrimPrefix(v, "https://"), "http://")
	v = strings.TrimPrefix(strings.TrimPrefix(v, "www."), "github.com/")
	parts := strings.Split(strings.Trim(v, "/"), "/")
	if len(parts) < 2 {
		return "", fail(400, "A GitHub repo is owner/name")
	}
	repo := parts[0] + "/" + strings.TrimSuffix(parts[1], ".git")
	if !repoRe.MatchString(repo) || strings.HasSuffix(repo, "/.") || strings.HasSuffix(repo, "/..") {
		return "", fail(400, "A GitHub repo is owner/name")
	}
	return repo, nil
}

func httpsURL(v string) string {
	v = strings.TrimSpace(v)
	u, err := url.Parse(v)
	if err != nil || u.Scheme != "https" || u.Host == "" || len(v) > 500 {
		return ""
	}
	return u.String()
}

// cleanConfig keeps the fields Slopetanium knows, each checked: nothing else from the handheld is stored
func cleanConfig(in map[string]any) map[string]any {
	out := map[string]any{}
	str := func(k string, n int) {
		if v, ok := in[k].(string); ok {
			if c := cleanText(v, n, false); c != "" {
				out[k] = c
			}
		}
	}
	str("originals_dir", 120)
	str("originals_pattern", 200)
	str("asset_pattern", 120)
	str("port_name", 120)
	str("hint", 400)
	if v, ok := in["prereleases"].(bool); ok {
		out["prereleases"] = v
	}
	switch v := in["keep_on_uninstall"].(type) {
	case string:
		out["keep_on_uninstall"] = cleanText(v, 200, false)
	case []any:
		keep := []string{}
		for _, x := range v {
			if s, ok := x.(string); ok && len(keep) < 10 {
				if c := cleanText(s, 60, false); c != "" {
					keep = append(keep, c)
				}
			}
		}
		out["keep_on_uninstall"] = keep
	}
	if g, ok := in["gog"].(map[string]any); ok {
		if id, ok := g["id"].(float64); ok && id > 0 && id < 1e12 {
			gg := map[string]any{"id": int64(id)}
			for _, k := range []string{"title", "dest", "files"} {
				if s, ok := g[k].(string); ok {
					gg[k] = cleanText(s, 120, false)
				}
			}
			out["gog"] = gg
		}
	}
	if b, ok := in["buy"].([]any); ok {
		buy := []map[string]string{}
		for _, x := range b {
			if m, ok := x.(map[string]any); ok && len(buy) < 4 {
				store, _ := m["store"].(string)
				u, _ := m["url"].(string)
				if u = httpsURL(u); u != "" && storeHost(u) && cleanText(store, 40, false) != "" {
					buy = append(buy, map[string]string{"store": cleanText(store, 40, false), "url": u})
				}
			}
		}
		if len(buy) > 0 {
			out["buy"] = buy
		}
	}
	if rn, ok := in["rename"].(map[string]any); ok {
		ren := map[string]string{}
		for k, v := range rn {
			if s, ok := v.(string); ok && len(ren) < 5 {
				ren[cleanText(k, 60, false)] = cleanText(s, 60, false)
			}
		}
		if len(ren) > 0 {
			out["rename"] = ren
		}
	}
	return out
}

// stores a "Buy on" link may point to (a sharer could otherwise send people anywhere)
var storeHosts = []string{"gog.com", "store.steampowered.com", "steampowered.com", "humblebundle.com", "itch.io",
	"zoom-platform.com", "store.epicgames.com", "ea.com", "ubisoft.com", "amazon.com", "fanatical.com", "greenmangaming.com"}

func storeHost(u string) bool {
	pu, err := url.Parse(u)
	if err != nil {
		return false
	}
	h := strings.ToLower(pu.Hostname())
	for _, s := range storeHosts {
		if h == s || strings.HasSuffix(h, "."+s) {
			return true
		}
	}
	return false
}

// portID: a listed port (reviews and installs are for those only)
func portID(db *sql.DB, repo string) (int64, error) {
	var id int64
	err := db.QueryRow(`SELECT id FROM ports p WHERE p.repo = ? AND `+visible, repo).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, fail(404, "Not a community port")
	}
	return id, err
}

// ------------------------------------------------------------------ handlers

// GET /v1/ports?q=&sort=popular|rating|new|updated|name&limit=&offset=
func (s *Server) handleList(w http.ResponseWriter, r *http.Request) error {
	q := r.URL.Query()
	limit, _ := strconv.Atoi(q.Get("limit"))
	if limit <= 0 || limit > 100 {
		limit = 60
	}
	offset, _ := strconv.Atoi(q.Get("offset"))
	if offset < 0 {
		offset = 0
	}
	ports, err := listPorts(s.db, q.Get("q"), q.Get("sort"), limit, offset)
	if err != nil {
		return err
	}
	writeJSON(w, 200, map[string]any{"ports": ports})
	return nil
}

func (s *Server) detail(repo, viewer string) (*PortDetail, error) {
	row := s.db.QueryRow(`SELECT p.id, p.config, p.igdb, `+summaryCols+` FROM ports p WHERE p.repo = ? AND `+visible, repo)
	var d PortDetail
	var cfg, game string
	sum, err := scanSummary(row, &d.id, &cfg, &game)
	d.PortSummary = sum
	if err == nil && game != "" {
		d.Game = &Game{}
		_ = json.Unmarshal([]byte(game), d.Game)
	}
	if errors.Is(err, sql.ErrNoRows) {
		return nil, fail(404, "Not a community port")
	}
	if err != nil {
		return nil, err
	}
	if d.Title == "" {
		d.Title = d.Repo[strings.Index(d.Repo, "/")+1:]
	}
	_ = json.Unmarshal([]byte(cfg), &d.Config)
	rows, err := s.db.Query(`SELECT r.id, a.nick, r.author, r.rating, r.text, r.version, r.device, r.created, r.updated
		FROM reviews r JOIN authors a ON a.id = r.author
		WHERE r.port_id = ? AND r.hidden = 0 AND a.banned = 0 ORDER BY r.updated DESC LIMIT 200`, d.id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	d.ReviewList = []Review{}
	for rows.Next() {
		var rv Review
		if err := rows.Scan(&rv.ID, &rv.Nick, &rv.author, &rv.Rating, &rv.Text, &rv.Version, &rv.Device, &rv.Created, &rv.Updated); err != nil {
			return nil, err
		}
		if rv.Nick == "" {
			rv.Nick = nickFor(rv.author)
		}
		rv.Mine = viewer != "" && rv.author == viewer
		d.Hist[rv.Rating-1]++
		d.ReviewList = append(d.ReviewList, rv)
	}
	if d.ByVersion, err = counts(s.db, `SELECT tag, COUNT(*) FROM pulls WHERE port_id = ? GROUP BY tag ORDER BY 2 DESC LIMIT 12`, d.id); err != nil {
		return nil, err
	}
	if d.ByDevice, err = counts(s.db, `SELECT CASE WHEN device = '' THEN 'other' ELSE device END, COUNT(*) FROM pulls WHERE port_id = ? GROUP BY 1 ORDER BY 2 DESC LIMIT 12`, d.id); err != nil {
		return nil, err
	}
	if d.ByCFW, err = counts(s.db, `SELECT CASE WHEN cfw = '' THEN 'other' ELSE cfw END, COUNT(*) FROM pulls WHERE port_id = ? GROUP BY 1 ORDER BY 2 DESC LIMIT 12`, d.id); err != nil {
		return nil, err
	}
	_ = s.db.QueryRow(`SELECT COUNT(*) FROM pulls WHERE port_id = ? AND day >= ?`, d.id, now()/86400-7).Scan(&d.Week)
	return &d, nil
}

// GET /v1/ports/{owner}/{name}
func (s *Server) handleDetail(w http.ResponseWriter, r *http.Request) error {
	viewer := ""
	if r.Header.Get("X-Slop-Device") != "" {
		viewer, _ = s.author(r)
	}
	d, err := s.detail(r.PathValue("owner")+"/"+r.PathValue("name"), viewer)
	if err != nil {
		return err
	}
	writeJSON(w, 200, d)
	return nil
}

// POST /v1/ports {repo, title, description, cover_url, config}: share a port
func (s *Server) handleShare(w http.ResponseWriter, r *http.Request) error {
	author, err := s.author(r)
	if err != nil {
		return err
	}
	if err := s.limitWrite(r, author); err != nil {
		return err
	}
	var in struct {
		Repo        string         `json:"repo"`
		Title       string         `json:"title"`
		Description string         `json:"description"`
		CoverURL    string         `json:"cover_url"`
		Config      map[string]any `json:"config"`
	}
	if err := readBody(r, &in); err != nil {
		return err
	}
	repo, err := repoFrom(in.Repo)
	if err != nil {
		return err
	}
	var id int64
	var verified int
	err = s.db.QueryRow(`SELECT id, verified FROM ports WHERE repo = ?`, repo).Scan(&id, &verified)
	if errors.Is(err, sql.ErrNoRows) {
		// new to the community: GitHub must know it, with a release zip
		if !s.newPorts.allow(clientIP(r)) {
			return fail(429, "That's a lot of new ports for one day; try again tomorrow")
		}
		info, gerr := s.gh.Fetch(repo)
		if gerr != nil && errors.Is(gerr, errNotFound) {
			return fail(400, "GitHub has no such public repo: "+repo)
		}
		if gerr != nil && errors.Is(gerr, errNoZip) {
			return fail(400, repo+" has no release with a port .zip yet")
		}
		if gerr != nil {
			// nothing is listed that GitHub hasn't confirmed
			return fail(503, "GitHub can't be asked right now; try sharing again later")
		}
		cfg, _ := json.Marshal(cleanConfig(in.Config))
		res, err := s.db.Exec(`INSERT INTO ports (repo, title, description, cover_url, config, created) VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(repo) DO NOTHING`,
			info.Repo, cleanText(in.Title, 80, false), cleanText(in.Description, 600, true), httpsURL(in.CoverURL), string(cfg), now())
		if err != nil {
			return err
		}
		if n, _ := res.RowsAffected(); n == 0 { // shared by someone else a moment ago
			if err := s.db.QueryRow(`SELECT id FROM ports WHERE repo = ?`, info.Repo).Scan(&id); err != nil {
				return err
			}
		} else {
			id, _ = res.LastInsertId()
			if err := s.gh.store(s.db, id, info); err != nil {
				return err
			}
			logf("shared %s (new)", info.Repo)
			s.igdb.poke()
		}
		_ = s.db.QueryRow(`SELECT repo, verified FROM ports WHERE id = ?`, id).Scan(&repo, &verified) // GitHub's spelling
	} else if err != nil {
		return err
	} else {
		// already in: an empty field gets the sharer's value, nothing is overwritten
		cfg, _ := json.Marshal(cleanConfig(in.Config))
		_, err = s.db.Exec(`UPDATE ports SET
			title = CASE WHEN title = '' THEN ? ELSE title END,
			description = CASE WHEN description = '' THEN ? ELSE description END,
			cover_url = CASE WHEN cover_url = '' THEN ? ELSE cover_url END,
			config = CASE WHEN config = '{}' THEN ? ELSE config END WHERE id = ?`,
			cleanText(in.Title, 80, false), cleanText(in.Description, 600, true), httpsURL(in.CoverURL), string(cfg), id)
		if err != nil {
			return err
		}
	}
	if _, err := s.db.Exec(`INSERT OR IGNORE INTO shares (port_id, author, at) VALUES (?, ?, ?)`, id, author, now()); err != nil {
		return err
	}
	var hidden int
	_ = s.db.QueryRow(`SELECT hidden FROM ports WHERE id = ?`, id).Scan(&hidden)
	writeJSON(w, 200, map[string]any{"repo": repo, "shared": true, "listed": verified == 1 && hidden == 0,
		"url": s.site.base + "/p/" + repo})
	return nil
}

// DELETE /v1/ports/{owner}/{name}/share: this handheld no longer shares it (the port stays listed while others do)
func (s *Server) handleUnshare(w http.ResponseWriter, r *http.Request) error {
	author, err := s.author(r)
	if err != nil {
		return err
	}
	var id int64
	if err := s.db.QueryRow(`SELECT id FROM ports WHERE repo = ?`, r.PathValue("owner")+"/"+r.PathValue("name")).Scan(&id); err != nil {
		writeJSON(w, 200, map[string]any{"shared": false})
		return nil
	}
	if _, err := s.db.Exec(`DELETE FROM shares WHERE port_id = ? AND author = ?`, id, author); err != nil {
		return err
	}
	writeJSON(w, 200, map[string]any{"shared": false})
	return nil
}

// POST /v1/ports/{owner}/{name}/reviews {rating, text, version, device}: one review per handheld, edited in place
func (s *Server) handleReview(w http.ResponseWriter, r *http.Request) error {
	author, err := s.author(r)
	if err != nil {
		return err
	}
	repo := r.PathValue("owner") + "/" + r.PathValue("name")
	id, err := portID(s.db, repo)
	if err != nil {
		return err
	}
	if err := s.limitWrite(r, author, s.reviews); err != nil {
		return err
	}
	if err != nil {
		return err
	}
	var in struct {
		Rating  int    `json:"rating"`
		Text    string `json:"text"`
		Version string `json:"version"`
		Device  string `json:"device"`
	}
	if err := readBody(r, &in); err != nil {
		return err
	}
	if in.Rating < 1 || in.Rating > 5 {
		return fail(400, "A rating is 1 to 5 stars")
	}
	text := cleanText(in.Text, 2000, true)
	t := now()
	_, err = s.db.Exec(`INSERT INTO reviews (port_id, author, rating, text, version, device, created, updated)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(port_id, author) DO UPDATE SET rating = excluded.rating, text = excluded.text,
			version = excluded.version, device = excluded.device, updated = excluded.updated`,
		id, author, in.Rating, text, cleanText(in.Version, 60, false), cleanText(in.Device, 60, false), t, t)
	if err != nil {
		return err
	}
	writeJSON(w, 200, map[string]any{"ok": true})
	return nil
}

// DELETE /v1/ports/{owner}/{name}/reviews: this handheld's own review
func (s *Server) handleUnreview(w http.ResponseWriter, r *http.Request) error {
	author, err := s.author(r)
	if err != nil {
		return err
	}
	var id int64
	if err := s.db.QueryRow(`SELECT id FROM ports WHERE repo = ?`, r.PathValue("owner")+"/"+r.PathValue("name")).Scan(&id); err != nil {
		writeJSON(w, 200, map[string]any{"ok": true})
		return nil
	}
	if _, err := s.db.Exec(`DELETE FROM reviews WHERE port_id = ? AND author = ?`, id, author); err != nil {
		return err
	}
	writeJSON(w, 200, map[string]any{"ok": true})
	return nil
}

// GET /v1/me: this handheld's nick, shares and reviews.  PUT /v1/me {nick}
func (s *Server) handleMe(w http.ResponseWriter, r *http.Request) error {
	author, err := s.author(r)
	if err != nil {
		return err
	}
	if r.Method == http.MethodPut {
		if err := s.limitWrite(r, author); err != nil {
			return err
		}
		var in struct {
			Nick string `json:"nick"`
		}
		if err := readBody(r, &in); err != nil {
			return err
		}
		nick := cleanText(in.Nick, 24, false)
		if nick != "" && utf8.RuneCountInString(nick) < 2 {
			return fail(400, "A name has at least 2 characters")
		}
		if nick != "" && !nickRe.MatchString(nick) {
			return fail(400, "A name uses letters A-Z, digits, spaces, dots, dashes and underscores")
		}
		if madeUpNick(nick) {
			return fail(400, "That looks like one of the made-up names; pick your own")
		}
		var taken int
		_ = s.db.QueryRow(`SELECT COUNT(*) FROM authors WHERE nick = ? COLLATE NOCASE AND id != ?`, nick, author).Scan(&taken)
		if nick != "" && taken > 0 {
			return fail(409, "Someone else uses that name")
		}
		if _, err := s.db.Exec(`UPDATE authors SET nick = ? WHERE id = ?`, nick, author); err != nil {
			return err
		}
	}
	var nick string
	_ = s.db.QueryRow(`SELECT nick FROM authors WHERE id = ?`, author).Scan(&nick)
	shares := []string{}
	rows, err := s.db.Query(`SELECT p.repo FROM shares s JOIN ports p ON p.id = s.port_id WHERE s.author = ? ORDER BY p.repo`, author)
	if err != nil {
		return err
	}
	for rows.Next() {
		var repo string
		_ = rows.Scan(&repo)
		shares = append(shares, repo)
	}
	rows.Close()
	reviews := map[string]map[string]any{}
	rows, err = s.db.Query(`SELECT p.repo, r.rating, r.text, r.updated, r.hidden FROM reviews r JOIN ports p ON p.id = r.port_id WHERE r.author = ?`, author)
	if err != nil {
		return err
	}
	for rows.Next() {
		var repo, text string
		var rating, hidden int
		var upd int64
		_ = rows.Scan(&repo, &rating, &text, &upd, &hidden)
		reviews[strings.ToLower(repo)] = map[string]any{"rating": rating, "text": text, "updated": upd, "hidden": hidden != 0}
	}
	rows.Close()
	writeJSON(w, 200, map[string]any{"nick": nick, "shown_as": firstNonEmpty(nick, nickFor(author)), "shares": shares, "reviews": reviews})
	return nil
}

// POST /v1/pulls {repo, tag, device, cfw}: an install, from handhelds whose owner said yes. Counted once per handheld
// and version, for community ports only; nothing about where it came from is kept.
func (s *Server) handlePull(w http.ResponseWriter, r *http.Request) error {
	if !s.pulls.allow(clientIP(r)) {
		return fail(429, "Too many")
	}
	author, err := s.author(r)
	if err != nil {
		return err
	}
	var in struct {
		Repo   string `json:"repo"`
		Tag    string `json:"tag"`
		Device string `json:"device"`
		CFW    string `json:"cfw"`
	}
	if err := readBody(r, &in); err != nil {
		return err
	}
	repo, err := repoFrom(in.Repo)
	if err != nil {
		return err
	}
	var id int64
	var tags string
	if err := s.db.QueryRow(`SELECT id, release_tags FROM ports p WHERE p.repo = ? AND `+visible, repo).Scan(&id, &tags); err != nil {
		writeJSON(w, 200, map[string]any{"counted": false})
		return nil
	}
	tag := cleanText(in.Tag, 80, false)
	if tag == "" {
		return fail(400, "Which version?")
	}
	known := []string{}
	_ = json.Unmarshal([]byte(tags), &known)
	found := false
	for _, k := range known {
		if k == tag {
			found = true
		}
	}
	if !found { // not a release of this port (that GitHub listed lately)
		writeJSON(w, 200, map[string]any{"counted": false})
		return nil
	}
	if err := s.touch(author); err != nil {
		return err
	}
	_, err = s.db.Exec(`INSERT OR IGNORE INTO pulls (port_id, tag, author, device, cfw, day) VALUES (?, ?, ?, ?, ?, ?)`,
		id, tag, author, cleanText(in.Device, 40, false), cleanText(in.CFW, 40, false), now()/86400)
	if err != nil {
		return err
	}
	writeJSON(w, 200, map[string]any{"counted": true})
	return nil
}

var nickRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9 ._-]*$`)

// madeUpNick: shaped like the names handhelds get without one ("Mint Otter 3f2a")
func madeUpNick(n string) bool {
	f := strings.Fields(strings.ToLower(n))
	if len(f) != 3 || len(f[2]) != 4 {
		return false
	}
	if _, err := strconv.ParseUint(f[2], 16, 16); err != nil {
		return false
	}
	in := func(list []string, w string) bool {
		for _, x := range list {
			if strings.ToLower(x) == w {
				return true
			}
		}
		return false
	}
	return in(nickA, f[0]) && in(nickB, f[1])
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if s != "" {
			return s
		}
	}
	return ""
}

// ------------------------------------------------------------------ names for the nameless

var nickA = []string{"Crystal", "Pixel", "Turbo", "Slope", "Neon", "Retro", "Mint", "Lucky", "Swift", "Brave", "Quiet", "Cosmic",
	"Amber", "Frosty", "Golden", "Rusty", "Sunny", "Velvet", "Wild", "Zippy", "Silver", "Hidden", "Mighty", "Tiny"}
var nickB = []string{"Fox", "Badger", "Otter", "Gecko", "Falcon", "Panda", "Lynx", "Moth", "Heron", "Koala", "Raven", "Yak",
	"Newt", "Bison", "Crab", "Dingo", "Ferret", "Ibis", "Lemur", "Marten", "Puffin", "Quokka", "Tapir", "Wombat"}

// nickFor: a stable made-up name for an author without one ("Mint Otter 3f2a")
func nickFor(author string) string {
	if len(author) < 8 {
		return "Someone"
	}
	a, _ := strconv.ParseUint(author[:4], 16, 32)
	b, _ := strconv.ParseUint(author[4:8], 16, 32)
	return nickA[int(a)%len(nickA)] + " " + nickB[int(b)%len(nickB)] + " " + author[8:12]
}

// ------------------------------------------------------------------ rate limits (memory only)

type bucket struct {
	tokens float64
	at     time.Time
}

type limiter struct {
	mu    sync.Mutex
	m     map[string]*bucket
	burst float64
	per   time.Duration // one token back every per
}

func newLimiter(burst int, per time.Duration) *limiter {
	l := &limiter{m: map[string]*bucket{}, burst: float64(burst), per: per}
	go func() {
		for range time.Tick(10 * time.Minute) {
			l.mu.Lock()
			for k, b := range l.m {
				if time.Since(b.at) > time.Duration(l.burst)*l.per {
					delete(l.m, k)
				}
			}
			l.mu.Unlock()
		}
	}()
	return l
}

func (l *limiter) allow(key string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	b := l.m[key]
	t := time.Now()
	if b == nil {
		b = &bucket{tokens: l.burst, at: t}
		l.m[key] = b
	}
	b.tokens += t.Sub(b.at).Seconds() / l.per.Seconds()
	if b.tokens > l.burst {
		b.tokens = l.burst
	}
	b.at = t
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}
