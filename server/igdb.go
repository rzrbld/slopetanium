// SPDX-License-Identifier: MIT
package main

// IGDB (igdb.com, through Twitch's app access): what a shared port's game is, for the website only. Each listed port
// is matched to its IGDB game by name (its title's "(Game Name)", its GOG title, the title itself), exact names only;
// `admin igdb` pins or clears a match by hand.

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"
)

type IGDB struct {
	id, secret string
	client     *http.Client
	tokenURL   string
	apiURL     string
	mu         sync.Mutex
	token      string
	until      time.Time
	kick       chan struct{}
}

// Game: what the website shows about a port's game (kept as JSON in ports.igdb)
type Game struct {
	ID           int64    `json:"id"`
	Name         string   `json:"name"`
	URL          string   `json:"url"`
	Summary      string   `json:"summary"`
	Year         int      `json:"year"`
	Genres       []string `json:"genres"`
	Themes       []string `json:"themes"`
	Modes        []string `json:"modes"`
	Perspectives []string `json:"perspectives"`
	Developers   []string `json:"developers"`
	Publishers   []string `json:"publishers"`
	Platforms    []string `json:"platforms"`
	Rating       float64  `json:"rating"`
	RatingCount  int      `json:"rating_count"`
	Cover        string   `json:"cover"`
	Screenshots  []string `json:"screenshots"`
	Tags         []string `json:"tags"`
}

func newIGDB(id, secret string) *IGDB {
	if id == "" || secret == "" {
		return nil
	}
	return &IGDB{id: id, secret: secret, client: &http.Client{Timeout: 20 * time.Second},
		tokenURL: "https://id.twitch.tv/oauth2/token", apiURL: "https://api.igdb.com/v4", kick: make(chan struct{}, 1)}
}

func (g *IGDB) bearer() (string, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.token != "" && time.Now().Before(g.until) {
		return g.token, nil
	}
	resp, err := g.client.PostForm(g.tokenURL, url.Values{"client_id": {g.id}, "client_secret": {g.secret}, "grant_type": {"client_credentials"}})
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var d struct {
		Token   string `json:"access_token"`
		Expires int64  `json:"expires_in"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&d); err != nil || d.Token == "" {
		return "", fmt.Errorf("IGDB sign-in failed (HTTP %d)", resp.StatusCode)
	}
	g.token, g.until = d.Token, time.Now().Add(time.Duration(d.Expires-300)*time.Second)
	return g.token, nil
}

func (g *IGDB) query(endpoint, body string, out any) error {
	tok, err := g.bearer()
	if err != nil {
		return err
	}
	req, _ := http.NewRequest("POST", g.apiURL+"/"+endpoint, strings.NewReader(body))
	req.Header.Set("Client-ID", g.id)
	req.Header.Set("Authorization", "Bearer "+tok)
	req.Header.Set("Accept", "application/json")
	resp, err := g.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == 401 {
		g.mu.Lock()
		g.token = ""
		g.mu.Unlock()
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("IGDB: HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

const gameFields = `fields name,url,summary,first_release_date,genres.name,themes.name,game_modes.name,player_perspectives.name,
involved_companies.company.name,involved_companies.developer,involved_companies.publisher,platforms.abbreviation,
total_rating,total_rating_count,cover.image_id,screenshots.image_id;`

type rawGame struct {
	ID       int64   `json:"id"`
	Name     string  `json:"name"`
	URL      string  `json:"url"`
	Summary  string  `json:"summary"`
	Released int64   `json:"first_release_date"`
	Rating   float64 `json:"total_rating"`
	Count    int     `json:"total_rating_count"`
	Genres   []named `json:"genres"`
	Themes   []named `json:"themes"`
	Modes    []named `json:"game_modes"`
	Persp    []named `json:"player_perspectives"`
	Plat     []struct {
		Abbr string `json:"abbreviation"`
	} `json:"platforms"`
	Companies []struct {
		Company   named `json:"company"`
		Developer bool  `json:"developer"`
		Publisher bool  `json:"publisher"`
	} `json:"involved_companies"`
	Cover struct {
		ImageID string `json:"image_id"`
	} `json:"cover"`
	Shots []struct {
		ImageID string `json:"image_id"`
	} `json:"screenshots"`
}

type named struct {
	Name string `json:"name"`
}

var nonWord = regexp.MustCompile(`[^a-z0-9]+`)

// norm: a name compared loosely ("Conflict: Desert Storm" = "conflict desert storm"; a leading "the" doesn't count)
func norm(s string) string {
	s = strings.ToLower(s)
	s = strings.Map(func(r rune) rune {
		if r > unicode.MaxASCII {
			return ' '
		}
		return r
	}, s)
	s = strings.TrimSpace(nonWord.ReplaceAllString(s, " "))
	return strings.TrimPrefix(s, "the ")
}

var parens = regexp.MustCompile(`\(([^()]{2,80})\)`)

// gameNames: what a port's game is probably called, best guess first
func gameNames(title, repo string, cfg map[string]any) []string {
	var out []string
	add := func(s string) {
		s = strings.TrimSpace(s)
		if s == "" {
			return
		}
		for _, o := range out {
			if norm(o) == norm(s) {
				return
			}
		}
		out = append(out, s)
	}
	for _, m := range parens.FindAllStringSubmatch(title, -1) {
		add(m[1])
	}
	if gg, ok := cfg["gog"].(map[string]any); ok {
		if t, ok := gg["title"].(string); ok {
			add(t)
		}
	}
	bare := strings.TrimSpace(parens.ReplaceAllString(title, ""))
	add(bare)
	// a "Re" remake name: ReRed Faction II -> Red Faction II, The ReThing -> The Thing
	for _, w := range []string{"Re", "The Re"} {
		if strings.HasPrefix(bare, w) && len(bare) > len(w)+1 && unicode.IsUpper(rune(bare[len(w)])) {
			add(strings.TrimPrefix(bare, w))
			if w == "The Re" {
				add("The " + strings.TrimPrefix(bare, w))
			}
		}
	}
	return out
}

func (g *IGDB) byID(id int64) (*rawGame, error) {
	var res []rawGame
	if err := g.query("games", fmt.Sprintf("%s where id = %d;", gameFields, id), &res); err != nil {
		return nil, err
	}
	if len(res) == 0 {
		return nil, nil
	}
	return &res[0], nil
}

// match: the IGDB game named exactly like one of names (the most rated one when several are); failing that, one named
// like it with a subtitle ("Vyruz" -> "Vyruz: Destruction of the Untel Empire"); or nil
func (g *IGDB) match(names []string) (*rawGame, error) {
	results := make([][]rawGame, len(names))
	best := func(hits []rawGame) *rawGame {
		sort.SliceStable(hits, func(i, j int) bool { return hits[i].Count > hits[j].Count })
		return &hits[0]
	}
	for i, n := range names {
		q := strings.ReplaceAll(n, `"`, " ")
		if err := g.query("games", fmt.Sprintf(`search "%s"; %s limit 20;`, q, gameFields), &results[i]); err != nil {
			return nil, err
		}
		var hits []rawGame
		for _, r := range results[i] {
			if norm(r.Name) == norm(n) {
				hits = append(hits, r)
			}
		}
		if len(hits) > 0 {
			return best(hits), nil
		}
	}
	for i, n := range names {
		var hits []rawGame
		for _, r := range results[i] {
			if k := strings.Index(r.Name, ":"); k > 0 && norm(r.Name[:k]) == norm(n) {
				hits = append(hits, r)
			}
		}
		if len(hits) > 0 {
			return best(hits), nil
		}
	}
	return nil, nil
}

var genreShort = map[string]string{
	"Role-playing (RPG)":                 "RPG",
	"Real Time Strategy (RTS)":           "RTS",
	"Turn-based strategy (TBS)":          "Turn-based strategy",
	"Hack and slash/Beat 'em up":         "Beat 'em up",
	"Point-and-click":                    "Point and click",
	"Card & Board Game":                  "Card and board",
	"Quiz/Trivia":                        "Quiz",
	"Massively Multiplayer Online (MMO)": "MMO",
	"Platform":                           "Platformer",
	"Sport":                              "Sports",
}
var themeShort = map[string]string{"Science fiction": "Sci-fi", "4X (explore, expand, exploit, and exterminate)": "4X"}

func short(m map[string]string, s string) string {
	if v, ok := m[s]; ok {
		return v
	}
	return s
}

func toGame(r *rawGame) *Game {
	gm := &Game{ID: r.ID, Name: r.Name, URL: r.URL, Summary: cleanText(r.Summary, 1500, true), Rating: r.Rating,
		RatingCount: r.Count, Cover: r.Cover.ImageID}
	if r.Released > 0 {
		gm.Year = time.Unix(r.Released, 0).UTC().Year()
	}
	for _, x := range r.Genres {
		gm.Genres = append(gm.Genres, short(genreShort, x.Name))
	}
	for _, x := range r.Themes {
		gm.Themes = append(gm.Themes, short(themeShort, x.Name))
	}
	for _, x := range r.Modes {
		gm.Modes = append(gm.Modes, x.Name)
	}
	for _, x := range r.Persp {
		gm.Perspectives = append(gm.Perspectives, x.Name)
	}
	for _, c := range r.Companies {
		if c.Developer {
			gm.Developers = append(gm.Developers, c.Company.Name)
		}
		if c.Publisher {
			gm.Publishers = append(gm.Publishers, c.Company.Name)
		}
	}
	for _, p := range r.Plat {
		if p.Abbr != "" {
			gm.Platforms = append(gm.Platforms, p.Abbr)
		}
	}
	for i, s := range r.Shots {
		if i == 6 {
			break
		}
		gm.Screenshots = append(gm.Screenshots, s.ImageID)
	}
	gm.Tags = tagsFor(gm)
	return gm
}

// tagsFor: a few words to browse by. FPS and the like first, then genres, the telling themes, multiplayer.
func tagsFor(gm *Game) []string {
	var tags []string
	seen := map[string]bool{}
	add := func(t string) {
		if t != "" && !seen[strings.ToLower(t)] && len(tags) < 8 {
			seen[strings.ToLower(t)] = true
			tags = append(tags, t)
		}
	}
	has := func(list []string, v string) bool {
		for _, x := range list {
			if strings.EqualFold(x, v) {
				return true
			}
		}
		return false
	}
	if has(gm.Genres, "Shooter") && has(gm.Perspectives, "First person") {
		add("FPS")
	}
	if has(gm.Genres, "Shooter") && has(gm.Perspectives, "Third person") {
		add("Third-person shooter")
	}
	for _, x := range gm.Genres {
		add(x)
	}
	for _, x := range gm.Themes {
		switch x {
		case "Action", "Sci-fi", "Horror", "Survival", "Stealth", "Warfare", "Open world", "Fantasy", "Historical", "Comedy", "Thriller", "Mystery":
			add(x)
		}
	}
	if has(gm.Modes, "Multiplayer") || has(gm.Modes, "Split screen") {
		add("Multiplayer")
	}
	if has(gm.Modes, "Co-operative") {
		add("Co-op")
	}
	return tags
}

// refresher: unmatched ports soon after they're shared, matched ones again every month
func (g *IGDB) refresher(db *sql.DB) {
	for {
		g.refreshDue(db)
		select {
		case <-g.kick:
		case <-time.After(10 * time.Minute):
		}
	}
}

func (g *IGDB) poke() {
	if g == nil {
		return
	}
	select {
	case g.kick <- struct{}{}:
	default:
	}
}

func (g *IGDB) refreshDue(db *sql.DB) {
	rows, err := db.Query(`SELECT id, repo, title, config, igdb_pin FROM ports WHERE hidden = 0 AND verified = 1 AND
		(igdb_at = 0 OR igdb_at < ?) ORDER BY igdb_at LIMIT 15`, now()-30*86400)
	if err != nil {
		logf("igdb: %v", err)
		return
	}
	type job struct {
		id               int64
		repo, title, cfg string
		pin              int64
	}
	var jobs []job
	for rows.Next() {
		var j job
		_ = rows.Scan(&j.id, &j.repo, &j.title, &j.cfg, &j.pin)
		jobs = append(jobs, j)
	}
	rows.Close()
	for _, j := range jobs {
		var raw *rawGame
		switch {
		case j.pin < 0: // cleared by hand
		case j.pin > 0:
			raw, err = g.byID(j.pin)
		default:
			var cfg map[string]any
			_ = json.Unmarshal([]byte(j.cfg), &cfg)
			title := j.title
			if title == "" {
				title = j.repo[strings.Index(j.repo, "/")+1:]
			}
			raw, err = g.match(gameNames(title, j.repo, cfg))
		}
		if err != nil {
			logf("igdb %s: %v", j.repo, err)
			return // offline or limited: later
		}
		if err := storeGame(db, j.id, raw); err != nil {
			logf("igdb %s: %v", j.repo, err)
		} else if raw != nil {
			logf("igdb %s: %s (%d)", j.repo, raw.Name, raw.ID)
		} else {
			logf("igdb %s: no match", j.repo)
		}
		time.Sleep(300 * time.Millisecond) // IGDB allows 4 requests a second
	}
}

func storeGame(db *sql.DB, portID int64, raw *rawGame) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	js := ""
	var gid int64
	var tags []string
	if raw != nil {
		gm := toGame(raw)
		b, _ := json.Marshal(gm)
		js, gid, tags = string(b), gm.ID, gm.Tags
	}
	if _, err := tx.Exec(`UPDATE ports SET igdb = ?, igdb_id = ?, igdb_at = ? WHERE id = ?`, js, gid, now(), portID); err != nil {
		return err
	}
	if _, err := tx.Exec(`DELETE FROM port_tags WHERE port_id = ?`, portID); err != nil {
		return err
	}
	for _, t := range tags {
		if _, err := tx.Exec(`INSERT OR IGNORE INTO port_tags (port_id, tag) VALUES (?, ?)`, portID, t); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// ------------------------------------------------------------------ IGDB pictures, through this server

var igdbImageID = regexp.MustCompile(`^[a-z0-9]{4,24}$`)
var igdbSizes = map[string]bool{"t_cover_big": true, "t_screenshot_med": true, "t_screenshot_big": true, "t_720p": true}

func (st *Site) handleIGDBImage(w http.ResponseWriter, r *http.Request) {
	size, id := r.PathValue("size"), strings.TrimSuffix(r.PathValue("file"), ".jpg")
	if !igdbSizes[size] || !igdbImageID.MatchString(id) {
		http.NotFound(w, r)
		return
	}
	// only pictures of listed ports' games
	var n int
	_ = st.s.db.QueryRow(`SELECT COUNT(*) FROM ports p WHERE `+visible+` AND p.igdb LIKE ?`, `%"`+id+`"%`).Scan(&n)
	if n == 0 {
		http.NotFound(w, r)
		return
	}
	key := size + "/" + id
	var ctype string
	var data []byte
	if err := st.s.db.QueryRow(`SELECT ctype, data FROM images WHERE key = ?`, key).Scan(&ctype, &data); err != nil || len(data) == 0 {
		ctype, data = fetchImage("https://images.igdb.com/igdb/image/upload/" + size + "/" + id + ".jpg")
		if len(data) == 0 {
			http.NotFound(w, r)
			return
		}
		_, _ = st.s.db.Exec(`INSERT OR REPLACE INTO images (key, ctype, data, fetched) VALUES (?, ?, ?, ?)`, key, ctype, data, now())
	}
	w.Header().Set("Content-Type", ctype)
	w.Header().Set("Cache-Control", "public, max-age=604800")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	_, _ = w.Write(data)
}
