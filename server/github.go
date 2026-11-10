// SPDX-License-Identifier: MIT
package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

var (
	errNotFound = errors.New("no such repo on GitHub")
	errNoZip    = errors.New("no release with a .zip")
)

type GitHub struct {
	token  string
	base   string // https://api.github.com (tests point it elsewhere)
	client *http.Client
}

type ghInfo struct {
	Repo      string // GitHub's own spelling of owner/name
	Desc      string
	Stars     int64
	Tag       string
	At        int64
	Size      int64
	Downloads int64
	Tags      []string // every release with a zip, newest first
}

func (g *GitHub) get(path string, v any) error {
	req, _ := http.NewRequest("GET", g.base+path, nil)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "slopetanium-community")
	if g.token != "" {
		req.Header.Set("Authorization", "Bearer "+g.token)
	}
	resp, err := g.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == 404 || resp.StatusCode == 451 {
		return errNotFound
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("GitHub: HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(v)
}

// Fetch: the repo and its newest release that carries a port .zip
func (g *GitHub) Fetch(repo string) (*ghInfo, error) {
	var r struct {
		FullName string `json:"full_name"`
		Desc     string `json:"description"`
		Stars    int64  `json:"stargazers_count"`
		Private  bool   `json:"private"`
	}
	if err := g.get("/repos/"+repo, &r); err != nil {
		return nil, err
	}
	if r.Private {
		return nil, errNotFound
	}
	var rels []struct {
		Tag       string    `json:"tag_name"`
		Draft     bool      `json:"draft"`
		Published time.Time `json:"published_at"`
		Assets    []struct {
			Name      string `json:"name"`
			Size      int64  `json:"size"`
			Downloads int64  `json:"download_count"`
		} `json:"assets"`
	}
	if err := g.get("/repos/"+r.FullName+"/releases?per_page=30", &rels); err != nil {
		return nil, err
	}
	info := &ghInfo{Repo: r.FullName, Desc: r.Desc, Stars: r.Stars}
	for _, rel := range rels {
		if rel.Draft {
			continue
		}
		for _, a := range rel.Assets {
			if strings.HasSuffix(strings.ToLower(a.Name), ".zip") {
				info.Downloads += a.Downloads
				if len(info.Tags) == 0 || info.Tags[len(info.Tags)-1] != rel.Tag {
					info.Tags = append(info.Tags, rel.Tag)
				}
				if info.Tag == "" {
					info.Tag, info.At, info.Size = rel.Tag, rel.Published.Unix(), a.Size
				}
			}
		}
	}
	if info.Tag == "" {
		return info, errNoZip
	}
	return info, nil
}

func (g *GitHub) store(db *sql.DB, id int64, info *ghInfo) error {
	tags, _ := json.Marshal(info.Tags)
	if _, err := db.Exec(`UPDATE ports SET release_tags = ? WHERE id = ?`, string(tags), id); err != nil {
		return err
	}
	_, err := db.Exec(`UPDATE ports SET repo = ?, gh_desc = ?, gh_stars = ?, gh_downloads = ?, latest_tag = ?, latest_at = ?,
		latest_size = ?, verified = 1, refreshed = ?, gh_error = '' WHERE id = ?`,
		info.Repo, cleanText(info.Desc, 300, false), info.Stars, info.Downloads, info.Tag, info.At, info.Size, now(), id)
	if err != nil && strings.Contains(err.Error(), "UNIQUE") {
		// renamed on GitHub into a repo that is shared too: keep the spelling we have
		_, err = db.Exec(`UPDATE ports SET gh_desc = ?, gh_stars = ?, gh_downloads = ?, latest_tag = ?, latest_at = ?,
			latest_size = ?, verified = 1, refreshed = ?, gh_error = '' WHERE id = ?`,
			cleanText(info.Desc, 300, false), info.Stars, info.Downloads, info.Tag, info.At, info.Size, now(), id)
	}
	return err
}

// refresher: every port's latest release and stars, every few hours; new shares GitHub couldn't check at once, sooner
func (g *GitHub) refresher(db *sql.DB, every time.Duration) {
	for {
		g.refreshDue(db, every)
		time.Sleep(5 * time.Minute)
	}
}

func (g *GitHub) refreshDue(db *sql.DB, every time.Duration) {
	rows, err := db.Query(`SELECT id, repo FROM ports WHERE hidden = 0 AND
		((verified = 1 AND refreshed < ?) OR (verified = 0 AND refreshed < ?)) ORDER BY refreshed LIMIT 20`,
		now()-int64(every.Seconds()), now()-86400)
	if err != nil {
		logf("refresh: %v", err)
		return
	}
	type job struct {
		id   int64
		repo string
	}
	var jobs []job
	for rows.Next() {
		var j job
		_ = rows.Scan(&j.id, &j.repo)
		jobs = append(jobs, j)
	}
	rows.Close()
	for _, j := range jobs {
		info, err := g.Fetch(j.repo)
		if err != nil {
			// gone or without zips: out of the list until it has one again (shares are kept)
			verified := 1
			if errors.Is(err, errNotFound) || errors.Is(err, errNoZip) {
				verified = 0
			}
			_, _ = db.Exec(`UPDATE ports SET refreshed = ?, gh_error = ?, verified = CASE WHEN ? = 0 THEN 0 ELSE verified END WHERE id = ?`,
				now(), err.Error(), verified, j.id)
			logf("refresh %s: %v", j.repo, err)
			if !errors.Is(err, errNotFound) && !errors.Is(err, errNoZip) {
				return // rate limited or offline: later
			}
			continue
		}
		if err := g.store(db, j.id, info); err != nil {
			logf("refresh %s: %v", j.repo, err)
		}
	}
}
