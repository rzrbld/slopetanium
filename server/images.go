// SPDX-License-Identifier: MIT
package main

// Pictures the site shows: box art from a port's cover_url, and IGDB's covers and screenshots. Fetched once from the
// public internet (never this machine or its network), resized, kept in SQLite and served from here.

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"image"
	_ "image/gif"
	"image/jpeg"
	_ "image/png"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"regexp"
	"sync"
	"syscall"
	"time"

	"golang.org/x/image/draw"
	_ "golang.org/x/image/webp"
)

const (
	maxDownload = 6 << 20  // bytes fetched at most
	maxPixels   = 40 << 20 // a bigger picture isn't decoded (a few KB of PNG can claim 50000x50000)
)

// ------------------------------------------------------------------ where the server may fetch from

// not the public internet: private, shared, reserved, documentation and transition ranges
var deniedNets = func() []netip.Prefix {
	var out []netip.Prefix
	for _, s := range []string{
		"0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
		"192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
		"::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:db8::/32",
		"2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
	} {
		out = append(out, netip.MustParsePrefix(s))
	}
	return out
}()

// this machine's own addresses: its public ones lead straight back to it
var ownAddrs = func() map[netip.Addr]bool {
	m := map[netip.Addr]bool{}
	if addrs, err := net.InterfaceAddrs(); err == nil {
		for _, a := range addrs {
			if n, ok := a.(*net.IPNet); ok {
				if ip, ok := netip.AddrFromSlice(n.IP); ok {
					m[ip.Unmap()] = true
				}
			}
		}
	}
	return m
}()

func publicAddr(ip netip.Addr) bool {
	ip = ip.Unmap()
	if !ip.IsValid() || !ip.IsGlobalUnicast() || ownAddrs[ip] {
		return false
	}
	for _, n := range deniedNets {
		if n.Contains(ip) {
			return false
		}
	}
	return true
}

// publicIP: kept for callers with a net.IP
func publicIP(ip net.IP) bool {
	a, ok := netip.AddrFromSlice(ip)
	return ok && publicAddr(a)
}

var imageClient = &http.Client{
	Timeout: 15 * time.Second,
	Transport: &http.Transport{
		Proxy: nil,
		DialContext: (&net.Dialer{Timeout: 10 * time.Second, Control: func(network, address string, _ syscall.RawConn) error {
			// the address actually dialled, on every hop: DNS can't point it elsewhere
			ap, err := netip.ParseAddrPort(address)
			if err != nil || ap.Port() != 443 || !publicAddr(ap.Addr()) {
				return fmt.Errorf("not a public https address: %s", address)
			}
			return nil
		}}).DialContext,
		TLSHandshakeTimeout: 10 * time.Second, MaxResponseHeaderBytes: 64 << 10, MaxIdleConns: 4,
	},
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if len(via) >= 3 {
			return errors.New("too many redirects")
		}
		if req.URL.Scheme != "https" || (req.URL.Port() != "" && req.URL.Port() != "443") {
			return errors.New("a redirect off https")
		}
		return nil
	},
}

var fetchSlots = make(chan struct{}, 3) // fetches at once

// fetchImage: a picture from a public https address, as a JPEG of at most maxW x maxH (smaller ones as they are),
// or nothing
func fetchImage(raw string, maxW, maxH int) (string, []byte) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Host == "" || (u.Port() != "" && u.Port() != "443") {
		return "", nil
	}
	fetchSlots <- struct{}{}
	defer func() { <-fetchSlots }()
	req, err := http.NewRequest("GET", u.String(), nil)
	if err != nil {
		return "", nil
	}
	req.Header.Set("User-Agent", "slopetanium-community (box art)")
	resp, err := imageClient.Do(req)
	if err != nil {
		logf("picture %s: %v", u.Host, err)
		return "", nil
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxDownload+1))
	if err != nil || resp.StatusCode != 200 || len(data) > maxDownload {
		logf("picture %s: HTTP %d, %d bytes", u.Host, resp.StatusCode, len(data))
		return "", nil
	}
	ct, out, err := shrink(data, maxW, maxH)
	if err != nil {
		logf("picture %s: %v", u.Host, err)
		return "", nil
	}
	return ct, out
}

// shrink: a decoded picture, scaled into maxW x maxH and stored as JPEG; anything else is refused
func shrink(data []byte, maxW, maxH int) (string, []byte, error) {
	cfg, format, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return "", nil, fmt.Errorf("not a picture")
	}
	if cfg.Width <= 0 || cfg.Height <= 0 || cfg.Width*cfg.Height > maxPixels {
		return "", nil, fmt.Errorf("%dx%d is too big", cfg.Width, cfg.Height)
	}
	if format == "jpeg" && cfg.Width <= maxW && cfg.Height <= maxH && len(data) <= 400<<10 {
		return "image/jpeg", data, nil // already fine
	}
	img, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return "", nil, fmt.Errorf("not a picture")
	}
	w, h := cfg.Width, cfg.Height
	if w > maxW || h > maxH {
		if w*maxH > h*maxW {
			h, w = h*maxW/w, maxW
		} else {
			w, h = w*maxH/h, maxH
		}
	}
	dst := image.NewRGBA(image.Rect(0, 0, max(w, 1), max(h, 1)))
	draw.Draw(dst, dst.Bounds(), image.White, image.Point{}, draw.Src) // (transparent parts on white)
	draw.CatmullRom.Scale(dst, dst.Bounds(), img, img.Bounds(), draw.Over, nil)
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, dst, &jpeg.Options{Quality: 85}); err != nil {
		return "", nil, err
	}
	return "image/jpeg", buf.Bytes(), nil
}

// ------------------------------------------------------------------ one fetch per picture

type flight struct {
	wg    sync.WaitGroup
	ctype string
	data  []byte
}

var (
	flightsMu sync.Mutex
	flights   = map[string]*flight{}
	missMu    sync.Mutex
	misses    = map[string]time.Time{} // pictures that weren't there, not asked for again for a while
)

// fetchOnce: concurrent asks for the same picture share one download
func fetchOnce(key string, fn func() (string, []byte)) (string, []byte) {
	missMu.Lock()
	if t, ok := misses[key]; ok && time.Since(t) < 30*time.Minute {
		missMu.Unlock()
		return "", nil
	}
	missMu.Unlock()
	flightsMu.Lock()
	if f, ok := flights[key]; ok {
		flightsMu.Unlock()
		f.wg.Wait()
		return f.ctype, f.data
	}
	f := &flight{}
	f.wg.Add(1)
	flights[key] = f
	flightsMu.Unlock()
	f.ctype, f.data = fn()
	f.wg.Done()
	flightsMu.Lock()
	delete(flights, key)
	flightsMu.Unlock()
	if len(f.data) == 0 {
		missMu.Lock()
		if len(misses) > 5000 {
			misses = map[string]time.Time{}
		}
		misses[key] = time.Now()
		missMu.Unlock()
	}
	return f.ctype, f.data
}

func servePicture(w http.ResponseWriter, r *http.Request, ctype string, data []byte, at int64, maxAge int) {
	sum := sha256.Sum256(data)
	w.Header().Set("Content-Type", ctype)
	w.Header().Set("Cache-Control", fmt.Sprintf("public, max-age=%d", maxAge))
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("ETag", `"`+hex.EncodeToString(sum[:8])+`"`)
	http.ServeContent(w, r, "", time.Unix(at, 0), bytes.NewReader(data))
}

// ------------------------------------------------------------------ the handlers

// handleCover: a listed port's box art (from its cover_url), else a redirect to its game's IGDB cover
func (st *Site) handleCover(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("owner") + "/" + r.PathValue("name")
	var id int64
	var src, game string
	if err := st.s.db.QueryRow(`SELECT p.id, p.cover_url, COALESCE(json_extract(CASE WHEN p.igdb = '' THEN '{}' ELSE p.igdb END, '$.cover'), '')
		FROM ports p WHERE p.repo = ? AND `+visible, repo).Scan(&id, &src, &game); err != nil {
		http.NotFound(w, r)
		return
	}
	if src == "" {
		if game == "" || !igdbImageID.MatchString(game) {
			http.NotFound(w, r)
			return
		}
		http.Redirect(w, r, "/igdb/t_cover_big/"+game+".jpg", http.StatusFound)
		return
	}
	var have, ctype string
	var data []byte
	var fetched int64
	err := st.s.db.QueryRow(`SELECT url, ctype, data, fetched FROM covers WHERE port_id = ?`, id).Scan(&have, &ctype, &data, &fetched)
	if err != nil || have != src || (len(data) == 0 && now()-fetched > 3600) {
		ctype, data = fetchOnce("cover:"+src, func() (string, []byte) { return fetchImage(src, 600, 900) })
		fetched = now()
		_, _ = st.s.db.Exec(`INSERT INTO covers (port_id, url, ctype, data, fetched) VALUES (?, ?, ?, ?, ?)
			ON CONFLICT(port_id) DO UPDATE SET url = excluded.url, ctype = excluded.ctype, data = excluded.data, fetched = excluded.fetched`,
			id, src, ctype, data, fetched)
	}
	if len(data) == 0 {
		http.NotFound(w, r)
		return
	}
	servePicture(w, r, ctype, data, fetched, 86400)
}

var igdbImageID = regexp.MustCompile(`^[a-z0-9]{4,24}$`)
var igdbSizes = map[string][2]int{"t_cover_big": {264, 374}, "t_screenshot_med": {569, 320}, "t_screenshot_big": {889, 500}}

// handleIGDBImage: an IGDB picture of a listed port's game (its cover or one of its screenshots, exactly)
func (st *Site) handleIGDBImage(w http.ResponseWriter, r *http.Request) {
	size, id := r.PathValue("size"), r.PathValue("file")
	if len(id) < 5 || id[len(id)-4:] != ".jpg" {
		http.NotFound(w, r)
		return
	}
	id = id[:len(id)-4]
	dims, ok := igdbSizes[size]
	if !ok || !igdbImageID.MatchString(id) {
		http.NotFound(w, r)
		return
	}
	var n int
	_ = st.s.db.QueryRow(`SELECT COUNT(*) FROM ports p WHERE `+visible+` AND p.igdb != '' AND
		(json_extract(p.igdb, '$.cover') = ? OR EXISTS (SELECT 1 FROM json_each(p.igdb, '$.screenshots') WHERE value = ?))`, id, id).Scan(&n)
	if n == 0 {
		http.NotFound(w, r)
		return
	}
	key := size + "/" + id
	var ctype string
	var data []byte
	var fetched int64
	if err := st.s.db.QueryRow(`SELECT ctype, data, fetched FROM images WHERE key = ?`, key).Scan(&ctype, &data, &fetched); err != nil || len(data) == 0 {
		ctype, data = fetchOnce("igdb:"+key, func() (string, []byte) {
			return fetchImage("https://images.igdb.com/igdb/image/upload/"+key+".jpg", dims[0]*2, dims[1]*2)
		})
		if len(data) == 0 {
			http.NotFound(w, r)
			return
		}
		fetched = now()
		_, _ = st.s.db.Exec(`INSERT OR REPLACE INTO images (key, ctype, data, fetched) VALUES (?, ?, ?, ?)`, key, ctype, data, fetched)
	}
	servePicture(w, r, ctype, data, fetched, 604800)
}

// prunePictures: pictures of ports no longer listed, and IGDB pictures no listed game uses
func prunePictures(db *sql.DB) {
	_, _ = db.Exec(`DELETE FROM covers WHERE port_id NOT IN (SELECT p.id FROM ports p WHERE ` + visible + `)`)
	_, _ = db.Exec(`DELETE FROM images WHERE NOT EXISTS (SELECT 1 FROM ports p WHERE ` + visible + ` AND p.igdb != '' AND
		(json_extract(p.igdb, '$.cover') = substr(images.key, instr(images.key, '/') + 1) OR
		 EXISTS (SELECT 1 FROM json_each(p.igdb, '$.screenshots') WHERE value = substr(images.key, instr(images.key, '/') + 1))))`)
}
