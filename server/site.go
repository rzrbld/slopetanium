// SPDX-License-Identifier: MIT
package main

import (
	"embed"
	"fmt"
	"html/template"
	"io"
	"io/fs"
	"math"
	"net"
	"net/http"
	"strings"
	"sync"
	"syscall"
	"time"
)

//go:embed web/*.html web/static/*
var webFS embed.FS

// Site: the read-only website, rendered on the server (works without JavaScript, links preview well)
type Site struct {
	s     *Server
	base  string // https://slopetanium.pw
	pages map[string]*template.Template
	// Slopetanium's own newest release, for the download button (pre-releases count: that's what there is)
	relMu  sync.Mutex
	relTag string
	relAt  int64
	relURL string
}

const selfRepo = "rzrbld/slopetanium"

func (st *Site) watchRelease(every time.Duration) {
	for {
		if info, err := st.s.gh.Fetch(selfRepo); err == nil {
			st.relMu.Lock()
			st.relTag, st.relAt = info.Tag, info.At
			st.relURL = "https://github.com/" + info.Repo + "/releases/download/" + info.Tag + "/slopetanium.zip"
			st.relMu.Unlock()
		} else {
			logf("own release: %v", err)
		}
		time.Sleep(every)
	}
}

func (st *Site) release() (tag string, at int64, url string) {
	st.relMu.Lock()
	defer st.relMu.Unlock()
	if st.relURL == "" {
		return "", 0, "https://github.com/" + selfRepo + "/releases"
	}
	return st.relTag, st.relAt, st.relURL
}

var tints = [][2]string{{"#ff7a59", "#b8326a"}, {"#3ed9b0", "#1f6f8b"}, {"#6aa7ff", "#5b3fd1"}, {"#ffb547", "#d4542b"}, {"#c06bff", "#5038c7"}, {"#2bd4d4", "#227a52"}}

var funcs = template.FuncMap{
	"size": func(n int64) string {
		f := float64(n)
		for _, u := range []string{"B", "KB", "MB", "GB"} {
			if f < 1024 || u == "GB" {
				if u == "B" {
					return fmt.Sprintf("%d B", n)
				}
				if f < 10 {
					return fmt.Sprintf("%.1f %s", f, u)
				}
				return fmt.Sprintf("%.0f %s", f, u)
			}
			f /= 1024
		}
		return ""
	},
	"day": func(t int64) string {
		if t == 0 {
			return ""
		}
		return time.Unix(t, 0).UTC().Format("Jan 2, 2006")
	},
	"ago": func(t int64) string {
		d := time.Since(time.Unix(t, 0))
		switch {
		case d < time.Hour:
			return "just now"
		case d < 24*time.Hour:
			return fmt.Sprintf("%d h ago", int(d.Hours()))
		case d < 60*24*time.Hour:
			return fmt.Sprintf("%d d ago", int(d.Hours()/24))
		}
		return time.Unix(t, 0).UTC().Format("Jan 2006")
	},
	"num": func(n int64) string {
		if n >= 10000 {
			return fmt.Sprintf("%.1fk", float64(n)/1000)
		}
		return fmt.Sprint(n)
	},
	"float": func(i int) float64 { return float64(i) },
	"join":  func(v []string) string { return strings.Join(v, ", ") },
	"first": func(n int, v []string) []string {
		if len(v) > n {
			return v[:n]
		}
		return v
	},
	// whole rows of three
	"rows3": func(v []string) []string {
		if len(v) > 6 {
			v = v[:6]
		}
		if n := len(v) - len(v)%3; n >= 3 {
			return v[:n]
		}
		return v
	},
	"pct100": func(f float64) string { return fmt.Sprintf("%.0f", f) },
	"qs": func(q, sort, tag string) string {
		v := []string{}
		if q != "" {
			v = append(v, "q="+template.URLQueryEscaper(q))
		}
		if sort != "" && sort != "popular" {
			v = append(v, "sort="+sort)
		}
		if tag != "" {
			v = append(v, "tag="+template.URLQueryEscaper(tag))
		}
		if len(v) == 0 {
			return "/ports"
		}
		return "/ports?" + strings.Join(v, "&")
	},
	"one": func(f float64) string { return fmt.Sprintf("%.1f", f) },
	// stars: five, the filled ones as a percentage width (half stars and all)
	"starpct": func(f float64) int { return int(math.Round(f / 5 * 100)) },
	"initials": func(s string) string {
		out := ""
		for _, w := range strings.FieldsFunc(s, func(r rune) bool { return !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9') }) {
			out += strings.ToUpper(w[:1])
			if len(out) == 2 {
				break
			}
		}
		if out == "" {
			return "?"
		}
		return out
	},
	"tint": func(s string, i int) string {
		sum := 0
		for _, r := range s {
			sum += int(r)
		}
		return tints[sum%len(tints)][i]
	},
	"pct": func(n, of int64) int {
		if of <= 0 {
			return 0
		}
		return int(math.Round(float64(n) * 100 / float64(of)))
	},
	"maxof": func(cs []Count) int64 {
		var m int64
		for _, c := range cs {
			if c.N > m {
				m = c.N
			}
		}
		return m
	},
	"sum5": func(a [5]int64) int64 { return a[0] + a[1] + a[2] + a[3] + a[4] },
	"rev":  func() []int { return []int{5, 4, 3, 2, 1} },
	"at":   func(a [5]int64, i int) int64 { return a[i-1] },
	"str": func(v any) string {
		switch x := v.(type) {
		case string:
			return x
		case []any:
			p := []string{}
			for _, e := range x {
				p = append(p, fmt.Sprint(e))
			}
			return strings.Join(p, ", ")
		case nil:
			return ""
		}
		return fmt.Sprint(v)
	},
	"short": func(t string) string {
		t = strings.TrimPrefix(t, "portmaster-")
		if len(t) > 18 {
			return t[:17] + "…"
		}
		return t
	},
}

func newSite(s *Server, base string) *Site {
	site := &Site{s: s, base: strings.TrimRight(base, "/"), pages: map[string]*template.Template{}}
	for _, p := range []string{"landing", "index", "port", "about", "missing"} {
		site.pages[p] = template.Must(template.New("layout.html").Funcs(funcs).ParseFS(webFS, "web/layout.html", "web/"+p+".html"))
	}
	return site
}

func (st *Site) render(w http.ResponseWriter, status int, page string, data map[string]any) {
	data["Base"] = st.base
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=60")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.WriteHeader(status)
	if err := st.pages[page].Execute(w, data); err != nil {
		logf("render %s: %v", page, err)
	}
}

func (st *Site) handleLanding(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		st.render(w, 404, "missing", map[string]any{"Title": "Not found"})
		return
	}
	top, err := listPorts(st.s.db, "", "popular", 6, 0)
	if err != nil {
		logf("landing: %v", err)
	}
	tag, at, url := st.release()
	st.render(w, 200, "landing", map[string]any{"Title": "", "Top": top, "Release": tag, "ReleaseAt": at, "Download": url, "Landing": true})
}

func (st *Site) handleIndex(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	sort := q.Get("sort")
	if sort == "" {
		sort = "popular"
	}
	tag := q.Get("tag")
	ports, err := listPorts(st.s.db, q.Get("q"), sort, 100, 0, tag)
	if err != nil {
		logf("index: %v", err)
		http.Error(w, "Server error", 500)
		return
	}
	var total struct{ Ports, Pulls, Reviews int64 }
	_ = st.s.db.QueryRow(`SELECT (SELECT COUNT(*) FROM ports p WHERE `+visible+`),
		(SELECT COUNT(*) FROM pulls), (SELECT COUNT(*) FROM reviews r JOIN authors a ON a.id = r.author WHERE r.hidden = 0 AND a.banned = 0)`).
		Scan(&total.Ports, &total.Pulls, &total.Reviews)
	tags, _ := allTags(st.s.db)
	title := "Community ports"
	if tag != "" {
		title = tag + " ports"
	}
	st.render(w, 200, "index", map[string]any{"Title": title, "Ports": ports, "Q": q.Get("q"), "Sort": sort, "Total": total,
		"Tag": tag, "Tags": tags})
}

func (st *Site) handlePort(w http.ResponseWriter, r *http.Request) {
	d, err := st.s.detail(r.PathValue("owner")+"/"+r.PathValue("name"), "")
	if err != nil {
		st.render(w, 404, "missing", map[string]any{"Title": "Not found"})
		return
	}
	if d.Repo != r.PathValue("owner")+"/"+r.PathValue("name") {
		http.Redirect(w, r, "/p/"+d.Repo, http.StatusMovedPermanently)
		return
	}
	st.render(w, 200, "port", map[string]any{"Title": d.Title, "P": d})
}

func (st *Site) handleAbout(w http.ResponseWriter, r *http.Request) {
	st.render(w, 200, "about", map[string]any{"Title": "About"})
}

// handleCover: a port's box art, fetched once from its cover_url and served from here (fast, and visitors' browsers
// don't call third-party image hosts)
func (st *Site) handleCover(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("owner") + "/" + r.PathValue("name")
	var id int64
	var url string
	if err := st.s.db.QueryRow(`SELECT p.id, p.cover_url FROM ports p WHERE p.repo = ? AND `+visible, repo).Scan(&id, &url); err != nil || url == "" {
		http.NotFound(w, r)
		return
	}
	var ctype, have string
	var data []byte
	var fetched int64
	err := st.s.db.QueryRow(`SELECT url, ctype, data, fetched FROM covers WHERE port_id = ?`, id).Scan(&have, &ctype, &data, &fetched)
	stale := err != nil || have != url || (len(data) == 0 && now()-fetched > 3600)
	if stale {
		ctype, data = fetchImage(url)
		_, _ = st.s.db.Exec(`INSERT INTO covers (port_id, url, ctype, data, fetched) VALUES (?, ?, ?, ?, ?)
			ON CONFLICT(port_id) DO UPDATE SET url = excluded.url, ctype = excluded.ctype, data = excluded.data, fetched = excluded.fetched`,
			id, url, ctype, data, now())
	}
	if len(data) == 0 {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", ctype)
	w.Header().Set("Cache-Control", "public, max-age=86400")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	_, _ = w.Write(data)
}

// imageClient: public internet only (a cover_url comes from a handheld: never this machine or its network)
var imageClient = &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{
	DialContext: (&net.Dialer{Timeout: 10 * time.Second, Control: func(network, address string, _ syscall.RawConn) error {
		host, _, err := net.SplitHostPort(address)
		if err != nil {
			return err
		}
		ip := net.ParseIP(host)
		if ip == nil || !publicIP(ip) {
			return fmt.Errorf("not a public address: %s", host)
		}
		return nil
	}}).DialContext,
	TLSHandshakeTimeout: 10 * time.Second, MaxResponseHeaderBytes: 64 << 10,
}}

func publicIP(ip net.IP) bool {
	return !(ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsMulticast() || ip.IsUnspecified() || ip.IsInterfaceLocalMulticast() ||
		(ip.To4() != nil && (ip.To4()[0] == 100 && ip.To4()[1]&0xc0 == 64 || ip.To4()[0] == 0)))
}

// fetchImage: an image of at most 6 MB (jpeg, png, webp, gif), or nothing
func fetchImage(url string) (string, []byte) {
	if !strings.HasPrefix(url, "https://") {
		return "", nil
	}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return "", nil
	}
	req.Header.Set("User-Agent", "slopetanium-community (box art)")
	resp, err := imageClient.Do(req)
	if err != nil {
		logf("cover %s: %v", url, err)
		return "", nil
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 6<<20+1))
	if err != nil || resp.StatusCode != 200 || len(data) > 6<<20 {
		logf("cover %s: HTTP %d, %d bytes", url, resp.StatusCode, len(data))
		return "", nil
	}
	ct := http.DetectContentType(data)
	switch ct {
	case "image/jpeg", "image/png", "image/webp", "image/gif":
		return ct, data
	}
	logf("cover %s: not an image (%s)", url, ct)
	return "", nil
}

func (st *Site) static() http.Handler {
	sub, _ := fs.Sub(webFS, "web/static")
	fsrv := http.StripPrefix("/static/", http.FileServer(http.FS(sub)))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "public, max-age=604800")
		fsrv.ServeHTTP(w, r)
	})
}
