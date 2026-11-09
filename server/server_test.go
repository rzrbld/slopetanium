// SPDX-License-Identifier: MIT
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// a GitHub with two repos: one with a port zip, one without releases
func fakeGitHub(t *testing.T) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/repos/rzrbld/ReNFS5", "/repos/rzrbld/renfs5":
			json.NewEncoder(w).Encode(map[string]any{"full_name": "rzrbld/ReNFS5", "description": "Need for Speed: Porsche Unleashed", "stargazers_count": 12})
		case "/repos/rzrbld/ReNFS5/releases":
			json.NewEncoder(w).Encode([]any{
				map[string]any{"tag_name": "v2", "published_at": time.Now().Format(time.RFC3339), "assets": []any{
					map[string]any{"name": "renfs5.zip", "size": 30 << 20, "download_count": 40}}},
				map[string]any{"tag_name": "v1", "published_at": time.Now().Format(time.RFC3339), "assets": []any{
					map[string]any{"name": "renfs5.zip", "size": 29 << 20, "download_count": 60}, map[string]any{"name": "notes.txt"}}},
			})
		case "/repos/someone/empty":
			json.NewEncoder(w).Encode(map[string]any{"full_name": "someone/empty"})
		case "/repos/someone/empty/releases":
			w.Write([]byte("[]"))
		default:
			http.NotFound(w, r)
		}
	}))
}

type client struct {
	t   *testing.T
	url string
	dev string
}

func (c client) do(method, path string, body any) (int, map[string]any) {
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, c.url+path, rd)
	if c.dev != "" {
		req.Header.Set("X-Slop-Device", c.dev)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.t.Fatal(err)
	}
	defer resp.Body.Close()
	out := map[string]any{}
	raw, _ := io.ReadAll(resp.Body)
	if strings.HasPrefix(resp.Header.Get("Content-Type"), "application/json") {
		_ = json.Unmarshal(raw, &out)
	} else {
		out["html"] = string(raw)
	}
	return resp.StatusCode, out
}

func TestCommunity(t *testing.T) {
	gh := fakeGitHub(t)
	defer gh.Close()
	db, err := openDB(filepath.Join(t.TempDir(), "c.db"))
	if err != nil {
		t.Fatal(err)
	}
	g := &GitHub{base: gh.URL, client: gh.Client()}
	_, h := newServer(db, []byte("salt-for-tests-salt-for-tests-xx"), g, "https://example.test")
	srv := httptest.NewServer(h)
	defer srv.Close()
	a := client{t, srv.URL, strings.Repeat("a1", 32)}
	b := client{t, srv.URL, strings.Repeat("b2", 32)}
	anon := client{t, srv.URL, ""}

	// sharing needs a device; GitHub must know the repo and it must have a port zip
	if st, _ := anon.do("POST", "/v1/ports", map[string]any{"repo": "rzrbld/ReNFS5"}); st != 401 {
		t.Fatalf("anonymous share: %d", st)
	}
	if st, j := a.do("POST", "/v1/ports", map[string]any{"repo": "nobody/nothing"}); st != 400 {
		t.Fatalf("unknown repo: %d %v", st, j)
	}
	if st, j := a.do("POST", "/v1/ports", map[string]any{"repo": "someone/empty"}); st != 400 || !strings.Contains(j["error"].(string), "zip") {
		t.Fatalf("no zip: %d %v", st, j)
	}
	if st, _ := a.do("POST", "/v1/ports", map[string]any{"repo": "../../etc"}); st != 400 {
		t.Fatal("bad repo accepted")
	}
	st, j := a.do("POST", "/v1/ports", map[string]any{"repo": "https://github.com/rzrbld/renfs5", "title": "ReNFS5\x00 (NFS)",
		"cover_url": "javascript:alert(1)", "config": map[string]any{"originals_dir": "source", "evil": "x",
			"buy": []any{map[string]any{"store": "GOG", "url": "http://insecure"}}, "gog": map[string]any{"id": 123.0, "dest": "source"}}})
	if st != 200 || j["listed"] != true || j["repo"] != "rzrbld/ReNFS5" {
		t.Fatalf("share: %d %v", st, j)
	}
	// the list: GitHub's facts, the cleaned fields
	_, j = anon.do("GET", "/v1/ports", nil)
	ports := j["ports"].([]any)
	if len(ports) != 1 {
		t.Fatalf("list: %v", j)
	}
	p := ports[0].(map[string]any)
	if p["title"] != "ReNFS5 (NFS)" || p["cover_url"] != "" || p["latest_tag"] != "v2" || p["gh_downloads"].(float64) != 100 || p["stars"].(float64) != 12 {
		t.Fatalf("summary: %v", p)
	}
	_, d := anon.do("GET", "/v1/ports/rzrbld/ReNFS5", nil)
	cfg := d["config"].(map[string]any)
	if cfg["originals_dir"] != "source" || cfg["evil"] != nil || cfg["buy"] != nil || cfg["gog"].(map[string]any)["id"].(float64) != 123 {
		t.Fatalf("config: %v", cfg)
	}

	// reviews: one per handheld, edited in place; ratings 1..5
	if st, _ := a.do("POST", "/v1/ports/rzrbld/ReNFS5/reviews", map[string]any{"rating": 6}); st != 400 {
		t.Fatal("rating 6 accepted")
	}
	a.do("POST", "/v1/ports/rzrbld/ReNFS5/reviews", map[string]any{"rating": 3, "text": "ok", "version": "v1", "device": "RG DS"})
	a.do("POST", "/v1/ports/rzrbld/ReNFS5/reviews", map[string]any{"rating": 5, "text": "great\n\n\n\nnow <b>fast</b>", "version": "v2"})
	b.do("POST", "/v1/ports/rzrbld/ReNFS5/reviews", map[string]any{"rating": 4, "text": "nice"})
	_, d = b.do("GET", "/v1/ports/rzrbld/ReNFS5", nil)
	rl := d["review_list"].([]any)
	if len(rl) != 2 || d["rating"].(float64) != 4.5 || d["reviews"].(float64) != 2 {
		t.Fatalf("reviews: %v", d)
	}
	mine := 0
	for _, x := range rl {
		r := x.(map[string]any)
		if r["mine"] == true {
			mine++
			if r["text"] != "nice" {
				t.Fatal("wrong mine")
			}
		}
		if strings.Contains(r["text"].(string), "\n\n\n") {
			t.Fatal("blank lines kept")
		}
	}
	if mine != 1 {
		t.Fatalf("mine: %d", mine)
	}
	// names: a made-up one, then a chosen one (unique)
	_, me := a.do("GET", "/v1/me", nil)
	if !strings.Contains(me["shown_as"].(string), " ") || len(me["shares"].([]any)) != 1 {
		t.Fatalf("me: %v", me)
	}
	if st, _ := a.do("PUT", "/v1/me", map[string]any{"nick": "Razblade"}); st != 200 {
		t.Fatal("nick")
	}
	if st, _ := b.do("PUT", "/v1/me", map[string]any{"nick": "razblade"}); st != 409 {
		t.Fatal("nick taken twice")
	}

	// installs: once per handheld and version, community ports only
	for i := 0; i < 3; i++ {
		a.do("POST", "/v1/pulls", map[string]any{"repo": "rzrbld/ReNFS5", "tag": "v2", "device": "RG DS", "cfw": "BananaOS"})
	}
	b.do("POST", "/v1/pulls", map[string]any{"repo": "rzrbld/ReNFS5", "tag": "v2", "device": "RG40XX H", "cfw": "muOS"})
	b.do("POST", "/v1/pulls", map[string]any{"repo": "rzrbld/ReNFS5", "tag": "v1", "device": "RG40XX H", "cfw": "muOS"})
	if _, j := b.do("POST", "/v1/pulls", map[string]any{"repo": "other/port", "tag": "v1"}); j["counted"] != false {
		t.Fatal("pull of a non-community port counted")
	}
	_, d = anon.do("GET", "/v1/ports/rzrbld/ReNFS5", nil)
	if d["pulls"].(float64) != 3 || d["pulls_week"].(float64) != 3 || len(d["by_device"].([]any)) != 2 {
		t.Fatalf("pulls: %v", d)
	}

	// the website: list, port page (escaped), about, 404
	_, pg := anon.do("GET", "/ports", nil)
	if !strings.Contains(pg["html"].(string), "ReNFS5 (NFS)") {
		t.Fatal("index")
	}
	_, pg = anon.do("GET", "/p/rzrbld/ReNFS5", nil)
	html := pg["html"].(string)
	if !strings.Contains(html, "Razblade") || strings.Contains(html, "<b>fast</b>") || !strings.Contains(html, "RG40XX H") {
		t.Fatal("port page")
	}
	if st, _ := anon.do("GET", "/p/nobody/nothing", nil); st != 404 {
		t.Fatal("404")
	}
	_, pg = anon.do("GET", "/", nil)
	if h := pg["html"].(string); !strings.Contains(h, "always up to date") || !strings.Contains(h, "/p/rzrbld/ReNFS5") || !strings.Contains(h, "/releases") {
		t.Fatal("landing")
	}
	if st, _ := anon.do("GET", "/nope", nil); st != 404 {
		t.Fatal("unknown page")
	}
	if st, _ := anon.do("GET", "/about", nil); st != 200 {
		t.Fatal("about")
	}
	if st, _ := anon.do("GET", "/ports?q=%25&sort=rating", nil); st != 200 {
		t.Fatal("search")
	}

	// moderation: a banned handheld's reviews vanish and it can't write
	var author string
	db.QueryRow(`SELECT author FROM reviews WHERE text = 'nice'`).Scan(&author)
	if err := admin(db, []string{"ban", author}); err != nil {
		t.Fatal(err)
	}
	_, d = anon.do("GET", "/v1/ports/rzrbld/ReNFS5", nil)
	if d["reviews"].(float64) != 1 {
		t.Fatalf("banned review shown: %v", d["reviews"])
	}
	if st, _ := b.do("POST", "/v1/ports/rzrbld/ReNFS5/reviews", map[string]any{"rating": 1}); st != 403 {
		t.Fatal("banned wrote")
	}
	// unsharing: listed while anyone shares it
	b2 := client{t, srv.URL, strings.Repeat("c3", 32)}
	b2.do("POST", "/v1/ports", map[string]any{"repo": "rzrbld/ReNFS5"})
	a.do("DELETE", "/v1/ports/rzrbld/ReNFS5/share", nil)
	if _, j := anon.do("GET", "/v1/ports", nil); len(j["ports"].([]any)) != 1 {
		t.Fatal("gone while still shared")
	}
	b2.do("DELETE", "/v1/ports/rzrbld/ReNFS5/share", nil)
	if _, j := anon.do("GET", "/v1/ports", nil); len(j["ports"].([]any)) != 0 {
		t.Fatal("listed with no sharer")
	}
	// nothing that identifies a person is in the database
	var n int
	db.QueryRow(`SELECT COUNT(*) FROM authors WHERE id LIKE '%a1a1%'`).Scan(&n)
	if n != 0 {
		t.Fatal("device secret stored")
	}
}

func TestCoverFetchStaysPublic(t *testing.T) {
	for _, u := range []string{"https://127.0.0.1/x.png", "https://10.0.0.1/x.png", "https://[::1]/x.png", "https://169.254.169.254/latest", "http://example.com/x.png"} {
		if ct, data := fetchImage(u); ct != "" || data != nil {
			t.Fatalf("fetched %s", u)
		}
	}
}

func TestRateLimit(t *testing.T) {
	l := newLimiter(3, time.Hour)
	for i := 0; i < 3; i++ {
		if !l.allow("x") {
			t.Fatal("blocked early")
		}
	}
	if l.allow("x") || !l.allow("y") {
		t.Fatal("limit")
	}
}

func TestGameNames(t *testing.T) {
	cases := map[string][]string{
		"ReNFS5 (Need for Speed: Porsche Unleashed)": {"Need for Speed: Porsche Unleashed", "ReNFS5"},
		"The ReThing (The Thing)":                    {"The Thing", "The ReThing"},
		"ReRed Faction II":                           {"ReRed Faction II", "Red Faction II"},
	}
	for title, want := range cases {
		got := gameNames(title, "x/y", nil)
		for _, w := range want {
			found := false
			for _, g := range got {
				if g == w {
					found = true
				}
			}
			if !found {
				t.Fatalf("%q: %v lacks %q", title, got, w)
			}
		}
	}
	if g := gameNames("ReConflict", "x/y", map[string]any{"gog": map[string]any{"title": "Conflict: Desert Storm"}}); g[0] != "Conflict: Desert Storm" {
		t.Fatalf("gog title first: %v", g)
	}
	if norm("The Thing") != norm("thing") || norm("Conflict: Desert Storm") != "conflict desert storm" {
		t.Fatal("norm")
	}
}

func TestIGDB(t *testing.T) {
	gh := fakeGitHub(t)
	defer gh.Close()
	var searched []string
	ig := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/token" {
			w.Write([]byte(`{"access_token":"tok","expires_in":5000}`))
			return
		}
		if r.Header.Get("Authorization") != "Bearer tok" || r.Header.Get("Client-ID") != "cid" {
			w.WriteHeader(401)
			return
		}
		b, _ := io.ReadAll(r.Body)
		searched = append(searched, string(b))
		// a near miss first, then the game
		w.Write([]byte(`[{"id":1,"name":"Need for Speed: Porsche Unleashed Deluxe","total_rating_count":99},
			{"id":94,"name":"Need for Speed: Porsche Unleashed","summary":"Drive <b>Porsches</b>.","first_release_date":954288000,
			 "genres":[{"name":"Racing"},{"name":"Shooter"}],"player_perspectives":[{"name":"First person"}],
			 "game_modes":[{"name":"Single player"},{"name":"Multiplayer"}],"themes":[{"name":"Science fiction"}],
			 "involved_companies":[{"company":{"name":"EA Canada"},"developer":true},{"company":{"name":"Electronic Arts"},"publisher":true}],
			 "cover":{"image_id":"co9bt7"},"screenshots":[{"image_id":"sc6abc"}],"total_rating":81.4,"total_rating_count":40,
			 "url":"https://www.igdb.com/games/need-for-speed-porsche-unleashed"}]`))
	}))
	defer ig.Close()
	db, _ := openDB(filepath.Join(t.TempDir(), "c.db"))
	srvr, h := newServer(db, []byte("salt-for-tests-salt-for-tests-xx"), &GitHub{base: gh.URL, client: gh.Client()}, "https://example.test")
	srvr.igdb = &IGDB{id: "cid", secret: "sec", client: ig.Client(), tokenURL: ig.URL + "/token", apiURL: ig.URL, kick: make(chan struct{}, 1)}
	srv := httptest.NewServer(h)
	defer srv.Close()
	a := client{t, srv.URL, strings.Repeat("a1", 32)}
	anon := client{t, srv.URL, ""}
	a.do("POST", "/v1/ports", map[string]any{"repo": "rzrbld/ReNFS5", "title": "ReNFS5 (Need for Speed: Porsche Unleashed)"})
	srvr.igdb.refreshDue(db)
	if len(searched) == 0 || !strings.Contains(searched[0], `search "Need for Speed: Porsche Unleashed"`) {
		t.Fatalf("searched: %v", searched)
	}
	var gid int64
	db.QueryRow(`SELECT igdb_id FROM ports`).Scan(&gid)
	if gid != 94 {
		t.Fatalf("matched %d, want the exact name (94)", gid)
	}
	// the website: tags (FPS from shooter + first person), the panel, the tag filter
	_, pg := anon.do("GET", "/p/rzrbld/ReNFS5", nil)
	html := pg["html"].(string)
	for _, want := range []string{">FPS<", ">Racing<", ">Sci-fi<", ">Multiplayer<", "About the game", "EA Canada", "/igdb/t_screenshot_med/sc6abc.jpg", "IGDB.com", "Drive &lt;b&gt;Porsches&lt;/b&gt;."} {
		if !strings.Contains(html, want) {
			t.Fatalf("port page lacks %q", want)
		}
	}
	_, pg = anon.do("GET", "/ports?tag=Racing", nil)
	if !strings.Contains(pg["html"].(string), "/p/rzrbld/ReNFS5") {
		t.Fatal("tag filter lost the port")
	}
	_, pg = anon.do("GET", "/ports?tag=Horror", nil)
	if strings.Contains(pg["html"].(string), "/p/rzrbld/ReNFS5") {
		t.Fatal("tag filter kept the port")
	}
	// the handheld's API doesn't carry it
	_, d := anon.do("GET", "/v1/ports/rzrbld/ReNFS5", nil)
	if _, ok := d["tags"]; ok {
		t.Fatal("tags in the API")
	}
	if _, ok := d["game"]; ok {
		t.Fatal("game in the API")
	}
	// pictures only of listed games' ids
	if st, _ := anon.do("GET", "/igdb/t_cover_big/zzzz9999.jpg", nil); st != 404 {
		t.Fatal("unlisted picture served")
	}
	if st, _ := anon.do("GET", "/igdb/t_original/co9bt7.jpg", nil); st != 404 {
		t.Fatal("unknown size served")
	}
	// pinned by hand: none
	admin(db, []string{"igdb", "rzrbld/ReNFS5", "none"})
	srvr.igdb.refreshDue(db)
	_, pg = anon.do("GET", "/p/rzrbld/ReNFS5", nil)
	if strings.Contains(pg["html"].(string), "About the game") {
		t.Fatal("cleared game still shown")
	}
}
