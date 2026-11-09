// SPDX-License-Identifier: MIT

// slopetanium-community: the server behind slopetanium.pw. Slopetanium on handhelds shares ports, posts reviews and
// (when its owner agreed) counts installs here; the website shows them read-only.
//
//	slopetanium-community serve -listen 127.0.0.1:8787 -db /var/lib/slopetanium/community.db -salt-file ... -base https://slopetanium.pw
//	slopetanium-community admin -db ... list-reviews <owner/name> | hide-review <id> | show-review <id> |
//	                               hide-port <owner/name> | show-port <owner/name> | ban <author> | unban <author> | stats
package main

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

func logf(f string, a ...any) { log.Printf(f, a...) }

// salt: made once, kept next to the database (never in it); without it the author hashes can't be linked to handhelds
func loadSalt(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	if err == nil && len(strings.TrimSpace(string(b))) >= 32 {
		return []byte(strings.TrimSpace(string(b))), nil
	}
	if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return nil, err
	}
	s := hex.EncodeToString(buf)
	if err := os.WriteFile(path, []byte(s+"\n"), 0o600); err != nil {
		return nil, err
	}
	return []byte(s), nil
}

func newServer(db *sql.DB, salt []byte, gh *GitHub, base string) (*Server, http.Handler) {
	s := &Server{db: db, salt: salt, gh: gh,
		writes: newLimiter(40, 90*time.Second), byAuth: newLimiter(30, 2*time.Minute), pulls: newLimiter(120, 10*time.Second)}
	s.site = newSite(s, base)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/ports", s.api(s.handleList))
	mux.HandleFunc("POST /v1/ports", s.api(s.handleShare))
	mux.HandleFunc("GET /v1/ports/{owner}/{name}", s.api(s.handleDetail))
	mux.HandleFunc("DELETE /v1/ports/{owner}/{name}/share", s.api(s.handleUnshare))
	mux.HandleFunc("POST /v1/ports/{owner}/{name}/reviews", s.api(s.handleReview))
	mux.HandleFunc("DELETE /v1/ports/{owner}/{name}/reviews", s.api(s.handleUnreview))
	mux.HandleFunc("GET /v1/me", s.api(s.handleMe))
	mux.HandleFunc("PUT /v1/me", s.api(s.handleMe))
	mux.HandleFunc("POST /v1/pulls", s.api(s.handlePull))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := db.Ping(); err != nil {
			http.Error(w, "db", 500)
			return
		}
		w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("GET /robots.txt", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("User-agent: *\nDisallow: /v1/\nAllow: /\n"))
	})
	mux.Handle("GET /static/", s.site.static())
	mux.HandleFunc("GET /p/{owner}/{name}", s.site.handlePort)
	mux.HandleFunc("GET /cover/{owner}/{name}", s.site.handleCover)
	mux.HandleFunc("GET /igdb/{size}/{file}", s.site.handleIGDBImage)
	mux.HandleFunc("GET /about", s.site.handleAbout)
	mux.HandleFunc("GET /ports", s.site.handleIndex)
	mux.HandleFunc("GET /", s.site.handleLanding)
	return s, mux
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetPrefix("community: ")
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: slopetanium-community serve|admin [flags]")
		os.Exit(2)
	}
	cmd := os.Args[1]
	fl := flag.NewFlagSet(cmd, flag.ExitOnError)
	dbPath := fl.String("db", "community.db", "SQLite database")
	listen := fl.String("listen", "127.0.0.1:8787", "address (Caddy in front does TLS)")
	saltFile := fl.String("salt-file", "", "salt for author hashes (default: next to the database)")
	base := fl.String("base", "https://slopetanium.pw", "the site's public address")
	every := fl.Duration("refresh", 6*time.Hour, "how often each port is checked on GitHub")
	_ = fl.Parse(os.Args[2:])
	db, err := openDB(*dbPath)
	if err != nil {
		log.Fatal(err)
	}
	switch cmd {
	case "serve":
		if *saltFile == "" {
			*saltFile = *dbPath + ".salt"
		}
		salt, err := loadSalt(*saltFile)
		if err != nil {
			log.Fatal(err)
		}
		gh := &GitHub{token: os.Getenv("GITHUB_TOKEN"), base: "https://api.github.com", client: &http.Client{Timeout: 20 * time.Second}}
		srvr, h := newServer(db, salt, gh, *base)
		go gh.refresher(db, *every)
		go srvr.site.watchRelease(time.Hour)
		if srvr.igdb = newIGDB(os.Getenv("IGDB_CLIENT_ID"), os.Getenv("IGDB_CLIENT_SECRET")); srvr.igdb != nil {
			go srvr.igdb.refresher(db)
		} else {
			logf("no IGDB_CLIENT_ID / IGDB_CLIENT_SECRET: no game info on the website")
		}
		srv := &http.Server{Addr: *listen, Handler: h, ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 30 * time.Second,
			WriteTimeout: 60 * time.Second, IdleTimeout: 120 * time.Second, MaxHeaderBytes: 32 << 10}
		logf("listening on %s", *listen)
		log.Fatal(srv.ListenAndServe())
	case "admin":
		if err := admin(db, fl.Args()); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	default:
		fmt.Fprintln(os.Stderr, "unknown command", cmd)
		os.Exit(2)
	}
}

func admin(db *sql.DB, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("admin: list-reviews <repo> | hide-review <id> | show-review <id> | hide-port <repo> | show-port <repo> | ban <author> | unban <author> | igdb <repo> <id|auto|none> | stats | backup <file>")
	}
	arg := ""
	if len(args) > 1 {
		arg = args[1]
	}
	exec := func(q string, a ...any) error {
		res, err := db.Exec(q, a...)
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		fmt.Println(n, "changed")
		return nil
	}
	switch args[0] {
	case "list-reviews":
		rows, err := db.Query(`SELECT r.id, r.author, a.nick, r.rating, r.hidden, replace(substr(r.text, 1, 80), char(10), ' ')
			FROM reviews r JOIN ports p ON p.id = r.port_id JOIN authors a ON a.id = r.author WHERE p.repo = ? ORDER BY r.updated DESC`, arg)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id, rating, hidden int
			var author, nick, text string
			_ = rows.Scan(&id, &author, &nick, &rating, &hidden, &text)
			fmt.Printf("%d\t%s\t%s\t%d★\thidden=%d\t%s\n", id, author, firstNonEmpty(nick, nickFor(author)), rating, hidden, text)
		}
		return nil
	case "hide-review":
		return exec(`UPDATE reviews SET hidden = 1 WHERE id = ?`, arg)
	case "show-review":
		return exec(`UPDATE reviews SET hidden = 0 WHERE id = ?`, arg)
	case "hide-port":
		return exec(`UPDATE ports SET hidden = 1 WHERE repo = ?`, arg)
	case "show-port":
		return exec(`UPDATE ports SET hidden = 0 WHERE repo = ?`, arg)
	case "ban":
		return exec(`UPDATE authors SET banned = 1 WHERE id = ?`, arg)
	case "unban":
		return exec(`UPDATE authors SET banned = 0 WHERE id = ?`, arg)
	case "igdb":
		// igdb <owner/name> <id | auto | none>: pin a port's game by hand, or let it be matched again
		if len(args) < 3 {
			return fmt.Errorf("igdb <owner/name> <igdb id | auto | none>")
		}
		pin := int64(0)
		switch args[2] {
		case "auto":
		case "none":
			pin = -1
		default:
			if _, err := fmt.Sscan(args[2], &pin); err != nil || pin <= 0 {
				return fmt.Errorf("an IGDB game id, auto or none")
			}
		}
		return exec(`UPDATE ports SET igdb_pin = ?, igdb_at = 0 WHERE repo = ?`, pin, arg)
	case "backup":
		// a consistent copy while the server runs (SQLite's VACUUM INTO)
		if arg == "" {
			return fmt.Errorf("backup <file>")
		}
		_ = os.Remove(arg)
		if _, err := db.Exec(`VACUUM INTO ?`, arg); err != nil {
			return err
		}
		fmt.Println("backed up to", arg)
		return nil
	case "stats":
		var ports, shared, authors, reviews, pulls int
		_ = db.QueryRow(`SELECT (SELECT COUNT(*) FROM ports), (SELECT COUNT(*) FROM ports p WHERE `+visible+`),
			(SELECT COUNT(*) FROM authors), (SELECT COUNT(*) FROM reviews), (SELECT COUNT(*) FROM pulls)`).Scan(&ports, &shared, &authors, &reviews, &pulls)
		fmt.Printf("ports %d (listed %d), handhelds %d, reviews %d, installs %d\n", ports, shared, authors, reviews, pulls)
		return nil
	}
	return fmt.Errorf("unknown admin command %q", args[0])
}
