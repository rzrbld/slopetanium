// SPDX-License-Identifier: MIT
package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// The whole community in one SQLite file. Nothing here identifies a person: an author is a keyed hash of a random
// secret made on the handheld; no IP address, location or account is stored.
const schema = `
CREATE TABLE IF NOT EXISTS authors (
	id        TEXT PRIMARY KEY,           -- hex HMAC(salt, device secret), shortened
	nick      TEXT NOT NULL DEFAULT '',
	created   INTEGER NOT NULL,
	last_seen INTEGER NOT NULL,
	banned    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ports (
	id          INTEGER PRIMARY KEY,
	repo        TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- owner/name on GitHub
	title       TEXT NOT NULL DEFAULT '',
	description TEXT NOT NULL DEFAULT '',
	cover_url   TEXT NOT NULL DEFAULT '',
	config      TEXT NOT NULL DEFAULT '{}',             -- what Slopetanium needs to install it well (JSON)
	created     INTEGER NOT NULL,
	hidden      INTEGER NOT NULL DEFAULT 0,             -- taken down by the admin
	verified    INTEGER NOT NULL DEFAULT 0,             -- GitHub has a release with a zip
	gh_desc     TEXT NOT NULL DEFAULT '',
	gh_stars    INTEGER NOT NULL DEFAULT 0,
	gh_downloads INTEGER NOT NULL DEFAULT 0,
	latest_tag  TEXT NOT NULL DEFAULT '',
	latest_at   INTEGER NOT NULL DEFAULT 0,
	latest_size INTEGER NOT NULL DEFAULT 0,
	refreshed   INTEGER NOT NULL DEFAULT 0,
	gh_error    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS shares (
	port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
	author  TEXT NOT NULL,
	at      INTEGER NOT NULL,
	PRIMARY KEY (port_id, author)
);
CREATE TABLE IF NOT EXISTS reviews (
	id      INTEGER PRIMARY KEY,
	port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
	author  TEXT NOT NULL,
	rating  INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
	text    TEXT NOT NULL DEFAULT '',
	version TEXT NOT NULL DEFAULT '',
	device  TEXT NOT NULL DEFAULT '',
	created INTEGER NOT NULL,
	updated INTEGER NOT NULL,
	hidden  INTEGER NOT NULL DEFAULT 0,
	UNIQUE (port_id, author)
);
CREATE TABLE IF NOT EXISTS pulls (
	port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
	tag     TEXT NOT NULL,
	author  TEXT NOT NULL,
	device  TEXT NOT NULL DEFAULT '',
	cfw     TEXT NOT NULL DEFAULT '',
	day     INTEGER NOT NULL,             -- days since 1970: enough for "this week"
	PRIMARY KEY (port_id, tag, author)
);
CREATE TABLE IF NOT EXISTS covers (
	port_id INTEGER PRIMARY KEY REFERENCES ports(id) ON DELETE CASCADE,
	url     TEXT NOT NULL,                 -- the cover_url it was fetched from
	ctype   TEXT NOT NULL DEFAULT '',
	data    BLOB,                          -- empty: the fetch failed (tried again later)
	fetched INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS port_tags (
	port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
	tag     TEXT NOT NULL,
	PRIMARY KEY (port_id, tag)
);
CREATE TABLE IF NOT EXISTS images (
	key     TEXT PRIMARY KEY,              -- "t_cover_big/co9bt7": IGDB pictures, kept once fetched
	ctype   TEXT NOT NULL,
	data    BLOB NOT NULL,
	fetched INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pulls_port ON pulls(port_id);
CREATE INDEX IF NOT EXISTS reviews_port ON reviews(port_id);
`

func openDB(path string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_pragma=synchronous(NORMAL)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1) // one writer; reads are quick
	if _, err := db.Exec(schema); err != nil {
		return nil, fmt.Errorf("schema: %w", err)
	}
	// columns added later (an existing database gets them too)
	for _, col := range []string{
		`igdb TEXT NOT NULL DEFAULT ''`, // the game, as the website shows it (JSON)
		`igdb_id INTEGER NOT NULL DEFAULT 0`,
		`igdb_at INTEGER NOT NULL DEFAULT 0`,  // last looked up
		`igdb_pin INTEGER NOT NULL DEFAULT 0`, // set by hand: an IGDB id, or -1 for none
	} {
		if _, err := db.Exec(`ALTER TABLE ports ADD COLUMN ` + col); err != nil && !strings.Contains(err.Error(), "duplicate column") {
			return nil, fmt.Errorf("migrate: %w", err)
		}
	}
	return db, nil
}

func now() int64 { return time.Now().Unix() }

// PortSummary: a card on the website, a row in the handheld's Community page.
type PortSummary struct {
	Repo        string   `json:"repo"`
	Title       string   `json:"title"`
	Description string   `json:"description"`
	CoverURL    string   `json:"cover_url"`
	LatestTag   string   `json:"latest_tag"`
	LatestAt    int64    `json:"latest_at"`
	LatestSize  int64    `json:"latest_size"`
	Stars       int64    `json:"stars"`
	Downloads   int64    `json:"gh_downloads"`
	Pulls       int64    `json:"pulls"`
	Rating      float64  `json:"rating"`
	Reviews     int64    `json:"reviews"`
	Sharers     int64    `json:"sharers"`
	Created     int64    `json:"created"`
	Tags        []string `json:"-"` // from IGDB: the website only
	GameCover   string   `json:"-"`
}

const summaryCols = `p.repo, p.title, CASE WHEN p.description != '' THEN p.description ELSE p.gh_desc END, p.cover_url,
	p.latest_tag, p.latest_at, p.latest_size, p.gh_stars, p.gh_downloads,
	(SELECT COUNT(*) FROM pulls WHERE port_id = p.id),
	COALESCE((SELECT AVG(r.rating) FROM reviews r JOIN authors a ON a.id = r.author WHERE r.port_id = p.id AND r.hidden = 0 AND a.banned = 0), 0),
	(SELECT COUNT(*) FROM reviews r JOIN authors a ON a.id = r.author WHERE r.port_id = p.id AND r.hidden = 0 AND a.banned = 0),
	(SELECT COUNT(*) FROM shares WHERE port_id = p.id), p.created,
	COALESCE(json_extract(CASE WHEN p.igdb = '' THEN '{}' ELSE p.igdb END, '$.tags'), '[]'),
	COALESCE(json_extract(CASE WHEN p.igdb = '' THEN '{}' ELSE p.igdb END, '$.cover'), '')`

// visible: shared by someone, checked on GitHub, not taken down
const visible = `p.hidden = 0 AND p.verified = 1 AND EXISTS (SELECT 1 FROM shares WHERE port_id = p.id)`

func scanSummary(sc interface{ Scan(...any) error }, extra ...any) (PortSummary, error) {
	var s PortSummary
	var tags string
	dest := append(extra, &s.Repo, &s.Title, &s.Description, &s.CoverURL, &s.LatestTag, &s.LatestAt, &s.LatestSize, &s.Stars,
		&s.Downloads, &s.Pulls, &s.Rating, &s.Reviews, &s.Sharers, &s.Created, &tags, &s.GameCover)
	err := sc.Scan(dest...)
	_ = json.Unmarshal([]byte(tags), &s.Tags) // the game's own order: FPS first, then genres
	if s.Title == "" {
		s.Title = s.Repo[strings.Index(s.Repo, "/")+1:]
	}
	return s, err
}

func listPorts(db *sql.DB, q, sort string, limit, offset int, tag ...string) ([]PortSummary, error) {
	order := map[string]string{
		"popular": "10 DESC, 12 DESC, 1",
		"rating":  "11 DESC, 12 DESC, 1",
		"new":     "14 DESC",
		"updated": "6 DESC",
		"name":    "2 COLLATE NOCASE",
	}[sort]
	if order == "" {
		order = "10 DESC, 12 DESC, 1"
	}
	args := []any{}
	where := visible
	if q = strings.TrimSpace(q); q != "" {
		where += ` AND (p.repo LIKE ? ESCAPE '\' OR p.title LIKE ? ESCAPE '\' OR p.description LIKE ? ESCAPE '\' OR p.gh_desc LIKE ? ESCAPE '\')`
		like := "%" + strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(q) + "%"
		args = append(args, like, like, like, like)
	}
	if len(tag) > 0 && tag[0] != "" {
		where += ` AND EXISTS (SELECT 1 FROM port_tags t WHERE t.port_id = p.id AND t.tag = ? COLLATE NOCASE)`
		args = append(args, tag[0])
	}
	args = append(args, limit, offset)
	rows, err := db.Query(`SELECT `+summaryCols+` FROM ports p WHERE `+where+` ORDER BY `+order+` LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []PortSummary{}
	for rows.Next() {
		s, err := scanSummary(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

type Review struct {
	ID      int64  `json:"id"`
	Nick    string `json:"nick"`
	Rating  int    `json:"rating"`
	Text    string `json:"text"`
	Version string `json:"version"`
	Device  string `json:"device"`
	Created int64  `json:"created"`
	Updated int64  `json:"updated"`
	Mine    bool   `json:"mine,omitempty"`
	author  string
}

type Count struct {
	Key string `json:"key"`
	N   int64  `json:"n"`
}

type PortDetail struct {
	PortSummary
	Config     map[string]any `json:"config"`
	ReviewList []Review       `json:"review_list"`
	ByVersion  []Count        `json:"by_version"`
	ByDevice   []Count        `json:"by_device"`
	ByCFW      []Count        `json:"by_cfw"`
	Hist       [5]int64       `json:"stars_hist"` // ratings 1..5
	Week       int64          `json:"pulls_week"`
	Game       *Game          `json:"-"` // IGDB: the website only
	id         int64
}

// allTags: the tags of listed ports, most used first
func allTags(db *sql.DB) ([]Count, error) {
	return counts(db, `SELECT t.tag, COUNT(*) FROM port_tags t JOIN ports p ON p.id = t.port_id WHERE `+visible+`
		GROUP BY t.tag ORDER BY 2 DESC, 1 LIMIT 40`)
}

func counts(db *sql.DB, q string, args ...any) ([]Count, error) {
	rows, err := db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Count{}
	for rows.Next() {
		var c Count
		if err := rows.Scan(&c.Key, &c.N); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}
