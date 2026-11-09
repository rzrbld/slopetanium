// Slopetanium UI: the handheld's menu. The ROCKNIXDS Pixel engine (third_party/rnds) lays the screens out and
// animates them, banana-ui's software compositor (third_party/banana/soft) draws only what changed, and SDL shows the
// result with vsync: one window per display (two on a dual-screen handheld such as the RG DS: the port in detail on
// top, the shelf below; one screen shows the shelf and opens a port's page on A).
//
// The data and every action come from the Python core (app/slopetanium.py --serve) through its API on 127.0.0.1.
#include "../third_party/rnds/RndsUI.h"
#include "../third_party/banana/soft.h"
#include "../third_party/banana/json.h"
#include "net.h"

#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#define STBI_ONLY_JPEG
#include "../third_party/stb/stb_image.h"
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "../third_party/stb/stb_image_write.h"

#include <SDL.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <dirent.h>
#include <fstream>
#include <ifaddrs.h>
#include <map>
#include <memory>
#include <mutex>
#include <net/if.h>
#include <netinet/in.h>
#include <sstream>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <thread>
#include <unistd.h>
#include <vector>

using namespace rnds;
using banana::JPtr;
using banana::JVal;

static void logf(const char* fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	fprintf(stderr, "[slopetanium-ui] ");
	vfprintf(stderr, fmt, ap);
	fprintf(stderr, "\n");
	va_end(ap);
}

static double nowMs()
{
	return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

// ---- pictures: stb, as banana-ui decodes them -------------------------------------------------------------------
static Bitmap stbDecode(const std::string& path)
{
	int w, h, n;
	unsigned char* d = stbi_load(path.c_str(), &w, &h, &n, 4);
	Bitmap b;
	if (!d) return b;
	b.w = w; b.h = h;
	b.px.assign(d, d + (size_t)w * h * 4);
	stbi_image_free(d);
	return b;
}
static bool stbSize(const std::string& path, int& w, int& h)
{
	int n;
	return stbi_info(path.c_str(), &w, &h, &n) != 0;
}

// ---- the theme: Slopetanium's own colours, accent, background and logo over the engine ------------------------
namespace theme
{
	// 0xRRGGBB
	const uint32_t screenBg = 0x141821, screenBand = 0x222a38, screenFrame = 0x090b10;
	const uint32_t ink = 0xe8eef7, muted = 0x8e9ab0, line = 0x6e7a90, edge = 0xd9e3f2, red = 0xff4d5e;
	const uint32_t panelBand = 0x2c3546, panelBody = 0x151a24, panelLow = 0x090b10;
	const uint32_t accentBase = 0x3ed9b0, accentLight = 0xa8f5dd, accentDark = 0x137a5f;
	const uint32_t accentInk = 0x062a20;
	const uint32_t ok = 0x5fd38d, warn = 0xffcf5c;
	const uint32_t portTints[] = { 0x3ed9b0, 0x4a9bff, 0xe08a4e, 0xc25bd6, 0xe05a6a, 0x2bb3c0, 0xd4a72c, 0x8f7cf0 };

	void apply()
	{
		auto A = [](uint32_t rgb) { return 0xff000000u | rgb; };
		themeColors = {
			{ "screenBg", A(screenBg) }, { "screenBand", A(screenBand) }, { "screenFrame", A(screenFrame) },
			{ "ink", A(ink) }, { "muted", A(muted) }, { "line", A(line) }, { "edge", A(edge) }, { "navy", A(0xf4f8ff) },
			{ "red", A(red) },
			{ "panelBand", A(panelBand) }, { "panelBody", A(panelBody) }, { "panelLow", A(panelLow) },
			{ "chipBand", A(0x232b3a) }, { "chipBody", A(0x10141c) },
			{ "bubbleBand", A(0x2a3344) }, { "bubbleBody", A(0x171c27) }, { "bubbleLow", A(0x0c0f15) },
			{ "sheetBase", A(0x121620) }, { "trackBg", A(0x0c0f15) }, { "trackHi", A(0x2a3344) }, { "railBg", A(0x0c0f15) },
			{ "rule", A(0x3a4456) }, { "brandBg", A(0x1b2230) }, { "meterBg", A(0x10141c) }, { "frameBg", A(0x151a24) },
			{ "frameBorder", A(edge) }, { "faceMuted", A(0x3a4456) }, { "kbdBg", A(ink) }, { "kbdInk", A(screenBg) },
			{ "slotOn", A(0xffffff) }, { "slotDark", A(0x07080c) }, { "slotLip", A(0x3a4456) }, { "cartLabel", A(ink) },
		};
		setAccent(Accent{ accentBase, accentLight, accentDark, 0x7febcb, 0x0e5c47 });
		themeFiles.dither = false;	// our own background instead of the dots
	}

	// the background: a slate gradient, faint "slope" stripes and a soft light from the top left. Made at the screen's
	// size once and kept (conf/wall-WxH-top.png), so the engine only copies it
	std::string wall(const std::string& dir, int W, int H, float s, bool top)
	{
		char name[96];
		snprintf(name, sizeof name, "%s/wall-%dx%d-%s-v2.png", dir.c_str(), W, H, top ? "top" : "bottom");
		struct stat st;
		if (stat(name, &st) == 0) return name;
		std::vector<uint8_t> px((size_t)W * H * 3);
		float period = 12 * s, thick = std::max(1.0f, std::round(s));
		for (int y = 0; y < H; y++)
		{
			float gy = (float)y / H;
			for (int x = 0; x < W; x++)
			{
				float gx = (float)x / W;
				// gradient: lighter at the top, a cool light from the top left
				float r = 0x14, g = 0x18, b = 0x21;
				float lift = (1 - gy) * 6 + std::max(0.0f, 1 - std::hypot(gx * 1.2f, gy * 1.5f)) * 10;
				r += lift * 0.8f; g += lift * 0.95f; b += lift * 1.2f;
				// the slope stripes: thin diagonals, fading towards the bottom right
				float d = std::fmod((x + y) + 1000 * period, period);
				if (d < thick)
				{
					float a = (top ? 0.055f : 0.045f) * (1.15f - 0.6f * (gx + gy) / 2);
					r += (255 - r) * a; g += (255 - g) * a; b += (255 - b) * a;
				}
				uint8_t* p = &px[((size_t)y * W + x) * 3];
				p[0] = (uint8_t)std::min(255.0f, r); p[1] = (uint8_t)std::min(255.0f, g); p[2] = (uint8_t)std::min(255.0f, b);
			}
		}
		if (!stbi_write_png(name, W, H, 3, px.data(), W * 3)) return "";
		return name;
	}
}

// what Pixelify Sans can't draw, said with what it can
static std::string plainText(const std::string& in)
{
	static const std::pair<const char*, const char*> map[] = {
		{ "\u2192", "->" }, { "\u2190", "<-" }, { "\u2014", " - " }, { "\u2013", "-" }, { "\u2018", "'" }, { "\u2019", "'" },
		{ "\u201c", "\"" }, { "\u201d", "\"" }, { "\u2022", "\u00b7" }, { "\u00a0", " " }, { "\u2265", ">=" }, { "\u2264", "<=" },
		{ "\u00d7", "x" }, { "\u2248", "~" } };
	std::string s = in;
	for (auto& m : map)
		for (size_t at = s.find(m.first); at != std::string::npos; at = s.find(m.first, at + strlen(m.second)))
			s.replace(at, strlen(m.first), m.second);
	// markdown links: their words
	for (size_t at = s.find("]("); at != std::string::npos; at = s.find("]("))
	{
		size_t open = s.rfind('[', at), close = s.find(')', at);
		if (open == std::string::npos || close == std::string::npos) break;
		s = s.substr(0, open) + s.substr(open + 1, at - open - 1) + s.substr(close + 1);
	}
	return s;
}

static std::string humanSize(double n)
{
	const char* u[] = { "B", "KB", "MB", "GB" };
	int i = 0;
	while (n >= 1024 && i < 3) { n /= 1024; i++; }
	char b[32];
	if (i == 0) snprintf(b, sizeof b, "%d B", (int)n);
	else snprintf(b, sizeof b, "%.1f %s", n, u[i]);
	return b;
}

// ---- the core's state ---------------------------------------------------------------------------------------------
struct Rel { std::string tag, body; bool pre = false, installed = false, cached = false, asset = false; };
struct Port
{
	std::string id, title, repo, status, inst, latest, art, error, prev, portDir, keep;
	bool installed = false;
	double size = 0, origTotal = 0;
	std::vector<std::pair<std::string, double>> orig;
	std::string origDir, imported;
	std::string jobState, jobMsg;
	double jobProg = -1;
	bool gog = false, gogPresent = false;
	int gogOwned = -1;	// 1 owned, 0 not, -1 not known yet
	std::string gogTitle;
	std::vector<Rel> rels;
};
struct State
{
	std::vector<Port> ports;
	bool busy = false, webRunning = false, online = false;
	std::vector<std::string> urls;
	std::string pin;
	int updates = 0;
	bool gogLinked = false;
};

static State parseState(const std::string& text)
{
	State s;
	JPtr root = banana::parseJson(text);
	if (!root || !root->isObj()) return s;
	s.busy = root->flag("busy");
	if (const JVal* w = root->get("web"))
	{
		s.webRunning = w->flag("running");
		s.online = w->flag("online");
		s.pin = w->str("pin");
		if (const JVal* u = w->get("urls")) for (auto& e : u->arr) s.urls.push_back(e->s);
	}
	if (const JVal* g = root->get("gog_account")) s.gogLinked = g->flag("linked");
	if (const JVal* rs = root->get("repos"))
		for (auto& e : rs->arr)
		{
			const JVal& r = *e;
			Port p;
			p.id = r.str("id"); p.title = plainText(r.str("title")); p.repo = r.str("repo"); p.status = r.str("status");
			p.inst = r.str("installed_tag"); p.latest = r.str("latest_tag"); p.art = r.str("art"); p.error = r.str("error");
			p.prev = r.str("previous_tag"); p.portDir = r.str("port_dir");
			p.error = plainText(p.error);
			p.installed = r.flag("installed");
			p.size = r.num("size");
			if (const JVal* k = r.get("keep_on_uninstall")) for (auto& v : k->arr) p.keep += (p.keep.empty() ? "" : ", ") + v->s;
			if (const JVal* o = r.get("originals"))
			{
				p.origTotal = o->num("total"); p.origDir = o->str("dir"); p.imported = o->str("imported_hint");
				if (const JVal* f = o->get("files")) for (auto& v : f->arr) p.orig.push_back({ v->str("name"), v->num("size") });
			}
			if (const JVal* j = r.get("job"); j && j->isObj())
			{
				p.jobState = j->str("state"); p.jobMsg = plainText(j->str("message"));
				const JVal* pr = j->get("progress");
				p.jobProg = pr && pr->isNum() ? pr->n : -1;
			}
			if (const JVal* rl = r.get("releases"))
				for (auto& v : rl->arr)
				{
					Rel x;
					x.tag = v->str("tag"); x.body = plainText(v->str("body")); x.pre = v->flag("prerelease");
					x.installed = v->flag("installed"); x.cached = v->flag("cached");
					const JVal* a = v->get("asset");
					x.asset = a && a->isObj();
					p.rels.push_back(x);
				}
			if (const JVal* g = r.get("gog"); g && g->isObj())
			{
				p.gog = true;
				p.gogTitle = plainText(g->str("title"));
				p.gogPresent = g->flag("present");
				const JVal* o = g->get("owned");
				p.gogOwned = o && o->kind == JVal::BOOL ? (o->b ? 1 : 0) : -1;
			}
			if (p.status == "update" || p.status == "unknown_version") s.updates++;
			s.ports.push_back(p);
		}
	return s;
}

static float readBattery(bool& charging)
{
	charging = false;
	DIR* d = opendir("/sys/class/power_supply");
	if (!d) return -1;
	float lvl = -1;
	while (dirent* e = readdir(d))
	{
		if (e->d_name[0] == '.') continue;
		std::string base = std::string("/sys/class/power_supply/") + e->d_name;
		std::ifstream t(base + "/type");
		std::string type;
		if (!(t >> type) || type != "Battery") continue;
		std::ifstream c(base + "/capacity");
		int v;
		if (c >> v) lvl = std::max(0, std::min(100, v)) / 100.0f;
		std::ifstream st(base + "/status");
		std::string sv;
		if (st >> sv) charging = sv == "Charging" || sv == "Full";
		break;
	}
	closedir(d);
	return lvl;
}

static bool hasNetwork()
{
	ifaddrs* ifa = nullptr;
	if (getifaddrs(&ifa) != 0) return false;
	bool up = false;
	for (ifaddrs* i = ifa; i; i = i->ifa_next)
		if (i->ifa_addr && i->ifa_addr->sa_family == AF_INET && !(i->ifa_flags & IFF_LOOPBACK) && (i->ifa_flags & IFF_UP))
			up = true;
	freeifaddrs(ifa);
	return up;
}

// ---- the host -----------------------------------------------------------------------------------------------------
struct Host;

class Src : public Source
{
public:
	Host* h = nullptr;
	int systemCount() override { return 1; }
	SysInfo system(int) override;
	int gameCount() override;
	GameInfo game(int i) override;
	std::string clockText() override { time_t t = time(nullptr); char b[16]; strftime(b, sizeof b, "%H:%M", localtime(&t)); return b; }
	std::string dateText() override { time_t t = time(nullptr); char b[16]; strftime(b, sizeof b, "%m/%d", localtime(&t)); return b; }
	float battery() override;
	bool charging() override;
	std::string batteryMode() override { return "text"; }
	bool online() override;
	void selectSystem(int) override {}
	void selectGame(int i) override;
	int openLibrary(int) override { return 0; }
	void backToHome() override {}
	void launch(int) override {}
	void toggleSystemFav(int) override {}
	void toggleGameFav(int) override {}
	int resumeGame(int) override { return -1; }
	std::string emptyTitle() override { return "Connecting…"; }
	std::vector<std::pair<std::string, std::string>> hints(bool library, int, int game) override;
	bool hostOpen(int game) override;
	bool hostFaces(int game, std::string& lk, std::string& ll, std::string& rk, std::string& rl, int& rkind) override;
	bool hostFace(const std::string& key) override;
	std::string plateLabel(int game) override;
};

struct MenuItem { std::string label; std::function<void()> fn; uint32_t color = theme::ink; };
struct Menu
{
	std::string title;
	std::vector<std::pair<std::string, uint32_t>> lines;
	std::vector<MenuItem> items;
	int sel = 0;
	std::string notes;	// a release whose notes the top screen shows meanwhile (two screens)
};

struct Spr { TexPtr tex; int ox = 0, oy = 0; };

struct Host
{
	std::string assets, conf;
	State st;
	Src src;
	std::unique_ptr<banana::SoftBackend> be;
	std::unique_ptr<UI> ui;
	std::unique_ptr<slop::CoreLink> link;
	float s = 1;
	bool dual = false;
	int sel = 0;
	// overlays
	enum Mode { SHELF, MENU, CONNECT, NOTES } mode = SHELF;
	std::vector<Menu> menus;	// a stack: confirmations go on top of their menu
	std::string notesTag;
	double overlayAt = 0;
	std::string toast;
	double toastAt = 0;
	bool quit = false;
	// pictures and text of the overlays, uploaded once
	std::map<std::string, Spr> cache;
	std::shared_ptr<Font> R, M;
	struct Hit { float x, y, w, h; std::function<void()> fn; };
	std::vector<Hit> hits[2];
	std::map<std::string, std::vector<std::string>> qr;	// text -> rows
	std::mutex qrLock;
	uint32_t wakeEvent = 0;
	bool batteryCharging = false;
	float batteryLevel = -1;
	double batteryAt = -1e9;
	bool net = false;
	double netAt = -1e9;

	int itemCount() const { return (int)st.ports.size() + 1; }
	bool isConnect(int i) const { return i == (int)st.ports.size(); }
	const Port* port(int i) const { return i >= 0 && i < (int)st.ports.size() ? &st.ports[i] : nullptr; }

	void wake() { SDL_Event e{}; e.type = wakeEvent; SDL_PushEvent(&e); }
	void flash(const std::string& t) { toast = t; toastAt = nowMs(); }

	// ---- data ----
	void apply(const std::string& json)
	{
		// (before the first state there is nothing to stay on)
		std::string selId = !haveState ? "" : port(sel) ? port(sel)->id : (isConnect(sel) ? "*connect" : "");
		haveState = true;
		st = parseState(json);
		ui->dataChanged();
		// keep the cursor on the same entry when the list changed under it
		int want = sel;
		for (int i = 0; i < (int)st.ports.size(); i++) if (st.ports[i].id == selId) want = i;
		if (selId == "*connect") want = (int)st.ports.size();
		want = std::max(0, std::min(itemCount() - 1, want));
		if (getenv("SLOP_DEBUG")) logf("apply: sel %d want %d engine %d items %d", sel, want, ui->gameIndex(), itemCount());
		if (want != ui->gameIndex()) ui->jumpTo(want);
		sel = ui->gameIndex();
		if (st.ports.empty() && mode == SHELF && !connectShown) { connectShown = true; ui->jumpTo(itemCount() - 1); openConnect(); }
	}
	bool connectShown = false, haveState = false;

	// ---- actions ----
	std::string path(const Port& p, const std::string& what) { return "/api/repos/" + p.id + "/" + what; }
	void install(const Port& p, const std::string& tag)
	{
		link->post(path(p, "install"), "{\"tag\":\"" + tag + "\"}");
		flash("Queued " + p.title + " " + tag);
	}
	void openConnect()
	{
		if (!st.webRunning) link->post("/api/web/start");
		mode = CONNECT;
		overlayAt = nowMs();
	}
	void closeOverlay()
	{
		if (!menus.empty()) menus.pop_back();
		mode = menus.empty() ? SHELF : MENU;
		overlayAt = nowMs();
	}
	void pushMenu(Menu m)
	{
		menus.push_back(m);
		mode = MENU;
		overlayAt = nowMs();
	}
	void confirm(const std::string& title, std::vector<std::pair<std::string, uint32_t>> lines, const std::string& ok,
		std::function<void()> fn, uint32_t color = theme::ink, const std::string& notes = "")
	{
		Menu m;
		m.title = title; m.lines = lines; m.notes = notes;
		m.items = { { ok, [this, fn] { menus.clear(); mode = SHELF; fn(); }, color }, { "Cancel", nullptr, theme::muted } };
		pushMenu(m);
	}

	void openItem(int i)
	{
		if (isConnect(i)) { openConnect(); return; }
		const Port* pp = port(i);
		if (!pp) return;
		Port p = *pp;
		Menu m;
		m.title = p.title;
		m.lines = { { p.repo, theme::muted } };
		m.notes = p.latest;
		bool busy = p.jobState == "running" || p.jobState == "queued";
		if (busy) m.items.push_back({ "Working: " + (p.jobMsg.empty() ? p.jobState : p.jobMsg), nullptr, theme::line });
		else if (!p.latest.empty() && (p.status == "update" || p.status == "unknown_version"))
			m.items.push_back({ "Update to " + p.latest, [this, p] { menus.clear(); mode = SHELF; install(p, p.latest); } });
		else if (!p.latest.empty() && !p.installed)
			m.items.push_back({ "Install " + p.latest, [this, p] { menus.clear(); mode = SHELF; install(p, p.latest); } });
		if (p.installed && !p.prev.empty() && !busy)
			m.items.push_back({ "Roll back to " + p.prev, [this, p] {
				confirm("Roll back to " + p.prev + "?", { { (p.inst.empty() ? "?" : p.inst) + " -> " + p.prev, theme::accentBase },
					{ "Imported game files and saves are kept.", theme::muted } }, "Roll back", [this, p] { install(p, p.prev); }, theme::ink, p.prev);
			} });
		if (p.rels.size() > 1 && !busy)
			m.items.push_back({ "Other versions…", [this, p] { versions(p); } });
		if (!dual && !p.latest.empty())
			m.items.push_back({ "Release notes", [this, p] { notesTag = p.latest; mode = NOTES; overlayAt = nowMs(); } });
		if (p.gog && p.installed && !p.gogPresent && !busy)
		{
			if (!st.gogLinked)
				m.items.push_back({ "Game files: sign in to GOG on the web page", nullptr, theme::line });
			else if (p.gogOwned == 0)
				m.items.push_back({ (p.gogTitle.empty() ? std::string("The game") : p.gogTitle) + " is not in your GOG library", nullptr, theme::line });
			else
				m.items.push_back({ "Get game files from GOG", [this, p] { menus.clear(); mode = SHELF;
					link->post(path(p, "gog-download")); flash("Downloading " + (p.gogTitle.empty() ? p.title : p.gogTitle) + " from GOG"); } });
		}
		if (!p.orig.empty())
			m.items.push_back({ "Original files (" + humanSize(p.origTotal) + ")…", [this, p] { originals(p); } });
		m.items.push_back({ "Check for updates", [this, p] { menus.clear(); mode = SHELF; link->post(path(p, "check")); flash("Checking " + p.title); } });
		if (p.installed && !busy)
			m.items.push_back({ "Uninstall…", [this, p] { uninstall(p); }, theme::red });
		pushMenu(m);
	}
	void versions(const Port& p)
	{
		Menu m;
		m.title = "Versions";
		m.lines = { { p.title, theme::muted } };
		m.notes = p.latest;
		for (auto& r : p.rels)
		{
			if (!r.asset) continue;
			std::string label = r.tag + (r.pre ? "  pre" : "") + (r.installed ? "  · installed" : "") + (r.cached ? "  · on device" : "");
			if (r.installed) m.items.push_back({ label, nullptr, theme::ok });
			else
			{
				std::string tag = r.tag;
				m.items.push_back({ label, [this, p, tag] {
					confirm("Install " + tag + "?", { { (p.inst.empty() ? "-" : p.inst) + " -> " + tag, theme::accentBase },
						{ "Imported game files and saves are kept.", theme::muted } }, "Install", [this, p, tag] { install(p, tag); }, theme::ink, tag);
				} });
			}
		}
		pushMenu(m);
	}
	void originals(const Port& p)
	{
		std::vector<std::pair<std::string, uint32_t>> lines;
		lines.push_back({ std::to_string(p.orig.size()) + (p.orig.size() == 1 ? " item in ports/" : " items in ports/") + p.origDir, theme::muted });
		if (!p.imported.empty()) lines.push_back({ "Imported already (" + p.imported + "/): safe to delete.", theme::ok });
		else lines.push_back({ "Nothing imported yet: start the port once first!", theme::red });
		for (size_t i = 0; i < p.orig.size() && i < 3; i++) lines.push_back({ "  " + p.orig[i].first + "  " + humanSize(p.orig[i].second), theme::line });
		if (p.orig.size() > 3) lines.push_back({ "  …and " + std::to_string(p.orig.size() - 3) + " more", theme::line });
		confirm("Delete original files?", lines, "Delete " + humanSize(p.origTotal), [this, p] {
			link->post(path(p, "originals-delete"), "{}");
			flash("Freed " + humanSize(p.origTotal));
		}, theme::red);
	}
	void uninstall(const Port& p)
	{
		Menu m;
		m.title = "Uninstall?";
		m.lines = { { p.title, theme::muted }, { "Removes ports/" + p.portDir + " with its data and saves.", theme::warn } };
		m.items = {
			{ "Uninstall, back up " + (p.keep.empty() ? std::string("nothing") : p.keep), [this, p] { menus.clear(); mode = SHELF;
				link->post(path(p, "uninstall"), "{\"confirm\":\"uninstall\",\"backup\":true}"); flash("Uninstalling " + p.title); }, theme::red },
			{ "Uninstall, no backup", [this, p] { menus.clear(); mode = SHELF;
				link->post(path(p, "uninstall"), "{\"confirm\":\"uninstall\",\"backup\":false}"); flash("Uninstalling " + p.title); }, theme::red },
			{ "Cancel", nullptr, theme::muted } };
		pushMenu(m);
	}
	void checkAll() { link->post("/api/check"); flash("Checking GitHub for new releases…"); }
	void updateAll()
	{
		if (st.updates == 0) { flash("Nothing to update"); return; }
		link->post("/api/update-all");
		flash(std::to_string(st.updates) + (st.updates == 1 ? " update queued" : " updates queued"));
	}
	void tryQuit()
	{
		if (st.busy) flash("Still installing: exit when it's done");
		else quit = true;
	}
	void filesOf(int i)
	{
		const Port* p = port(i);
		if (p && !p->orig.empty()) originals(*p);
		else flash("No original files here");
	}

	// ---- input ----
	// buttons: "left" "right" "up" "down" "a" "b" "x" "y" "l" "r" "start"
	void button(const std::string& b)
	{
		if (mode == MENU && !menus.empty())
		{
			Menu& m = menus.back();
			int n = (int)m.items.size();
			if (b == "up") m.sel = (m.sel + n - 1) % n;
			else if (b == "down") m.sel = (m.sel + 1) % n;
			else if (b == "a")
			{
				auto& it = m.items[m.sel];
				if (it.fn) { auto fn = it.fn; fn(); }
				else if (it.label == "Cancel") closeOverlay();
			}
			else if (b == "b") closeOverlay();
			return;
		}
		if (mode == CONNECT || mode == NOTES)
		{
			if (b == "a" || b == "b") { mode = menus.empty() ? SHELF : MENU; overlayAt = nowMs(); }
			return;
		}
		if (b == "left") ui->press(UI::LEFT);
		else if (b == "right") ui->press(UI::RIGHT);
		else if (b == "l") ui->press(UI::L);
		else if (b == "r") ui->press(UI::R);
		else if (b == "up") ui->press(UI::UP);
		else if (b == "down") ui->press(UI::DOWN);
		else if (b == "a") ui->press(UI::A);	// -> hostOpen
		else if (b == "b") tryQuit();
		else if (b == "x") checkAll();
		else if (b == "y") filesOf(ui->gameIndex());
		else if (b == "start") updateAll();
		sel = ui->gameIndex();
		if (getenv("SLOP_DEBUG")) logf("button %s -> game %d (mode %d)", b.c_str(), sel, (int)mode);
	}

	// ---- overlay drawing (device px from CSS px) ----
	float X(float v) const { return v * s; }
	Spr boxSpr(const std::string& key, const BoxStyle& stl, float w, float h)
	{
		int dw = (int)std::lround(X(w)), dh = (int)std::lround(X(h));
		char k[48];
		snprintf(k, sizeof k, ":%dx%d", dw, dh);
		std::string key2 = "box:" + key + k;
		auto it = cache.find(key2);
		if (it != cache.end()) return it->second;
		PaintedBox pb = paintBox(stl, dw, dh);
		Spr sp{ be->upload(pb.bmp, true), pb.originX, pb.originY };
		cache[key2] = sp;
		return sp;
	}
	void drawSpr(const Spr& sp, float x, float y, float alpha = 1, const Color& tint = Color(1, 1, 1, 1))
	{
		if (!sp.tex) return;
		be->draw(sp.tex, std::round(X(x)) - sp.ox, std::round(X(y)) - sp.oy, (float)sp.tex->w, (float)sp.tex->h,
			Color(tint.r, tint.g, tint.b, tint.a * alpha));
	}
	BoxStyle style(float radius, float border, uint32_t borderC, uint32_t bg, std::vector<Band> bands, std::vector<Shadow> sh)
	{
		BoxStyle b;
		b.radius = X(radius);
		b.border = border <= 0 ? 0 : std::max(1.0f, std::floor(border * s));
		b.borderColor = Color::hex(borderC);
		b.bg = Color::hex(bg);
		for (auto& x : bands) b.bands.push_back(Band{ X(x.y0), x.y1 < 0 ? -1 : X(x.y1), x.c });
		for (auto& x : sh) b.shadows.push_back(Shadow{ X(x.dx), X(x.dy), X(x.spread), x.c, x.inset });
		return b;
	}
	// a box of any size from one small painting: its corners as they are, its edges and middle stretched (a nine-
	// slice). The first frame that shows a menu or a card costs no more than any other (a whole panel painted at its
	// size took a few hundred ms on the handheld, a hitch right when it opened)
	struct Nine { TexPtr t[9]; int cut = 0, ox = 0, oy = 0, w = 0, h = 0, padR = 0, padB = 0; };
	std::map<std::string, Nine> nines;
	void nine(const std::string& key, const BoxStyle& stl, float corner, float x, float y, float w, float h, float alpha)
	{
		auto it = nines.find(key);
		if (it == nines.end())
		{
			Nine n;
			n.cut = (int)std::ceil(X(corner));
			int dw = 2 * n.cut + 4, dh = 2 * n.cut + 4;	// the box painted just bigger than its corners
			PaintedBox pb = paintBox(stl, dw, dh);
			n.ox = pb.originX; n.oy = pb.originY; n.w = pb.bmp.w; n.h = pb.bmp.h;
			int xs[4] = { 0, n.ox + n.cut, n.ox + dw - n.cut, pb.bmp.w }, ys[4] = { 0, n.oy + n.cut, n.oy + dh - n.cut, pb.bmp.h };
			for (int j = 0; j < 3; j++)
				for (int i = 0; i < 3; i++)
					n.t[j * 3 + i] = be->upload(crop(pb.bmp, xs[i], ys[j], xs[i + 1] - xs[i], ys[j + 1] - ys[j]), true);
			it = nines.emplace(key, n).first;
		}
		const Nine& n = it->second;
		float bx = std::round(X(x)) - n.ox, by = std::round(X(y)) - n.oy;
		float bw = std::round(X(w)), bh = std::round(X(h));
		float cw[3] = { (float)(n.ox + n.cut), bw - 2 * n.cut, (float)(n.w - (n.ox + 2 * n.cut + 4) + n.cut) };
		float ch[3] = { (float)(n.oy + n.cut), bh - 2 * n.cut, (float)(n.h - (n.oy + 2 * n.cut + 4) + n.cut) };
		if (cw[1] < 0 || ch[1] < 0) return;
		Color tint(1, 1, 1, alpha);
		float yy = by;
		for (int j = 0; j < 3; j++)
		{
			float xx = bx;
			for (int i = 0; i < 3; i++)
			{
				if (cw[i] > 0 && ch[j] > 0) be->draw(n.t[j * 3 + i], xx, yy, cw[i], ch[j], tint);
				xx += cw[i];
			}
			yy += ch[j];
		}
	}
	void panel(const std::string&, float x, float y, float w, float h, float alpha)
	{
		BoxStyle b = style(18, 3, theme::edge, theme::panelBody, { Band{ 0, 8, Color::hex(theme::panelBand) } },
			{ Shadow{ 0, -6, 0, Color::hex(theme::panelLow), true }, Shadow{ 4, 4, 0, Color(0, 0, 0, 0.45f), false } });
		nine("panel", b, 20, x, y, w, h, alpha);
	}
	void pick(float x, float y, float w, float h, float alpha)
	{
		BoxStyle b = style(8, 3, theme::edge, theme::accentBase, { Band{ 0, 5, Color::hex(theme::accentLight) } },
			{ Shadow{ 0, -4, 0, Color::hex(theme::accentDark), true } });
		nine("pick", b, 10, x, y, w, h, alpha);
	}
	float textW(const std::string& t, bool medium, float size, float ls = 0, bool up = false)
	{
		TextStyle ts; ts.font = medium ? M : R; ts.size = size; ts.letterSpacing = ls; ts.upper = up;
		return layoutLine(utf8to32(t), ts).width;
	}
	// one line at (x, top) CSS px; maxW: cut with "…"
	float text(const std::string& t, float x, float top, float size, uint32_t color, bool medium = false, float maxW = 0,
		float alpha = 1, float ls = 0, bool up = false, bool centre = false)
	{
		if (t.empty()) return 0;
		TextStyle ts; ts.font = medium ? M : R; ts.size = size; ts.letterSpacing = ls; ts.upper = up;
		TextLine l = maxW > 0 ? layoutEllipsis(utf8to32(t), ts, (int)maxW) : layoutLine(utf8to32(t), ts);
		if (centre) x -= l.width / 2;
		float lh = ts.font->normalLineHeight(size);
		float asc = ts.font->ascent(size), desc = ts.font->descent(size);
		float base = asc + std::floor((lh - (asc + desc)) / 2);
		float bx = X(x), by = X(top);
		int ix = (int)std::floor(bx), iy = (int)std::floor(by);
		int qx = (int)std::lround((bx - ix) * 8), qy = (int)std::lround((by - iy) * 8);
		char k[64];
		snprintf(k, sizeof k, "|%d|%.2f|%.2f|%d|%d|%d", medium ? 1 : 0, size, ls, up ? 1 : 0, qx, qy);
		std::string key = "txt:" + t + k + (maxW > 0 ? "|" + std::to_string((int)maxW) : "");
		Spr sp;
		auto it = cache.find(key);
		if (it != cache.end()) sp = it->second;
		else
		{
			TextBitmap tb = renderLine(l, ts, s, base, qx / 8.0f, qy / 8.0f);
			sp = Spr{ be->upload(tb.bmp, true), tb.padX, tb.padY };
			cache[key] = sp;
		}
		Color c = Color::hex(color, alpha);
		if (sp.tex) be->draw(sp.tex, (float)(ix - sp.ox), (float)(iy - sp.oy), (float)sp.tex->w, (float)sp.tex->h, c);
		return l.width;
	}
	std::vector<std::string> wrap(const std::string& t, float size, float maxW, int maxLines, bool medium = false)
	{
		TextStyle ts; ts.font = medium ? M : R; ts.size = size;
		std::vector<std::string> out;
		for (auto& l : layoutWrap(utf8to32(t), ts, (int)maxW, maxLines))
		{
			std::string u;
			for (char32_t c : l.text)
			{
				if (c < 0x80) u += (char)c;
				else if (c < 0x800) { u += (char)(0xc0 | (c >> 6)); u += (char)(0x80 | (c & 63)); }
				else if (c < 0x10000) { u += (char)(0xe0 | (c >> 12)); u += (char)(0x80 | ((c >> 6) & 63)); u += (char)(0x80 | (c & 63)); }
				else { u += (char)(0xf0 | (c >> 18)); u += (char)(0x80 | ((c >> 12) & 63)); u += (char)(0x80 | ((c >> 6) & 63)); u += (char)(0x80 | (c & 63)); }
			}
			out.push_back(u);
		}
		return out;
	}
	void fill(float x, float y, float w, float h, uint32_t rgb, float a)
	{
		be->fill(std::round(X(x)), std::round(X(y)), std::round(X(w)), std::round(X(h)), Color::hex(rgb, a));
	}

	static float easeOut3(float p) { p = std::min(1.0f, std::max(0.0f, p)); return 1 - (1 - p) * (1 - p) * (1 - p); }
	float overlayP() const { return easeOut3((float)((nowMs() - overlayAt) / 220.0)); }
	bool overlayMoving() const { return nowMs() - overlayAt < 240 || (!toast.empty() && nowMs() - toastAt < 3200); }

	// the menu panel: x, y, w CSS px; maxRows rows
	void drawMenu(int screen, float x, float y, float w, float maxH)
	{
		Menu& m = menus.back();
		float p = overlayP();
		float rise = 10 * (1 - p), a = p;
		const float row = 34;
		int nl = (int)m.lines.size();
		int visible = std::max(1, std::min((int)m.items.size(), (int)((maxH - 64 - nl * 22) / row)));
		float h = 16 + 34 + nl * 22 + 8 + visible * row + 12;
		float py = y + rise;
		panel("menu" + std::to_string((int)h) + "x" + std::to_string((int)w), x, py, w, h, a);
		float ty = py + 16;
		text(m.title, x + 20, ty, 24, theme::ink, true, w - 40, a, 0.4f);
		ty += 34;
		for (auto& l : m.lines) { text(l.first, x + 20, ty, 17, l.second, false, w - 40, a); ty += 22; }
		ty += 8;
		int top = m.sel >= visible ? m.sel - visible + 1 : 0;
		for (int i = 0; i < visible && top + i < (int)m.items.size(); i++)
		{
			int idx = top + i;
			auto& it = m.items[idx];
			float ry = ty + i * row;
			if (idx == m.sel)
			{
				pick(x + 12, ry, w - 24, row - 4, a);
				text(it.label, x + 26, ry + 4, 20, theme::accentInk, true, w - 52, a);
			}
			else
				text(it.label, x + 26, ry + 4, 20, (it.fn || it.label == "Cancel") ? it.color : theme::line, true, w - 52, a);
			hits[screen].push_back(Hit{ x, ry, w, row, [this, idx] {
				Menu& mm = menus.back();
				if (mm.sel == idx) button("a"); else mm.sel = idx;
			} });
		}
		if ((int)m.items.size() > visible)
			text(std::to_string(m.sel + 1) + "/" + std::to_string(m.items.size()), x + w - 60, py + 20, 16, theme::muted, false, 0, a);
	}

	// over the top screen's sheet: an inner card
	void card(float alpha) { panel("card", 30, 72, 580, 382, alpha); }

	void drawNotes(const std::string& tag)
	{
		const Port* p = port(sel);
		if (!p) return;
		float a = overlayP();
		card(a);
		const Rel* rel = nullptr;
		for (auto& r : p->rels) if (r.tag == tag) rel = &r;
		text("What's new in " + tag, 52, 92, 22, theme::accentBase, true, 536, a);
		float y = 126;
		std::string body = rel ? rel->body : "";
		if (body.empty()) body = "No release notes.";
		std::istringstream in(body);
		std::string line;
		while (std::getline(in, line))
		{
			// markdown, plainly: no #, *, `, links as their words
			std::string o;
			for (size_t i = 0; i < line.size(); i++)
			{
				char c = line[i];
				if (c == '*' || c == '`' || c == '_' || c == '\r') continue;
				o += c;
			}
			size_t st0 = o.find_first_not_of(" #>");
			o = st0 == std::string::npos ? "" : o.substr(st0);
			if (o.rfind("- ", 0) == 0) o = "· " + o.substr(2);
			if (o.empty()) { y += 8; continue; }
			for (auto& l : wrap(o, 18, 536, 5))
			{
				if (y > 424) { text("…", 52, y - 4, 18, theme::muted, false, 0, a); return; }
				text(l, 52, y, 18, theme::ink, false, 0, a);
				y += 22;
			}
		}
	}

	void drawConnect()
	{
		float a = overlayP();
		card(a);
		const float L = 300, W = 290;
		if (!st.webRunning)
		{
			text("Starting the web interface…", 320, 250, 22, theme::muted, true, 0, a, 0, false, true);
			return;
		}
		std::string url = st.urls.empty() ? "" : st.urls[0];
		// the QR card where the cover goes
		BoxStyle qb = style(6, 3, theme::edge, 0xffffff, {}, { Shadow{ 4, 4, 0, Color(0, 0, 0, 0.4f), false } });
		nine("qrcard", qb, 8, 56, 156, 214, 214, a);
		if (!url.empty())
		{
			std::string full = url + "/?pin=" + st.pin;
			std::vector<std::string> rows;
			{
				std::lock_guard<std::mutex> l(qrLock);
				auto it = qr.find(full);
				if (it != qr.end()) rows = it->second;
				else
				{
					qr[full] = {};
					std::string enc;
					for (unsigned char c : full) { if (isalnum(c) || strchr("-._~", c)) enc += (char)c; else { char b[4]; snprintf(b, sizeof b, "%%%02X", c); enc += b; } }
					link->get("/api/qr?text=" + enc, [this, full](int code, const std::string& body) {
						if (code != 200) return;
						JPtr j = banana::parseJson(body);
						std::vector<std::string> r;
						if (j) if (const JVal* v = j->get("rows")) for (auto& e : v->arr) r.push_back(e->s);
						{ std::lock_guard<std::mutex> l(qrLock); qr[full] = r; }
						wake();
					});
				}
			}
			if (!rows.empty())
			{
				std::string key = "qr:" + full;
				auto it = cache.find(key);
				Spr sp;
				if (it != cache.end()) sp = it->second;
				else
				{
					int n = (int)rows.size();
					int cell = std::max(1, (int)std::floor(X(190) / n));
					Bitmap b;
					b.w = b.h = cell * n;
					b.px.assign((size_t)b.w * b.h * 4, 255);
					for (int y = 0; y < n; y++)
						for (int x = 0; x < n && x < (int)rows[y].size(); x++)
							if (rows[y][x] == '1')
								for (int yy = 0; yy < cell; yy++)
									for (int xx = 0; xx < cell; xx++)
									{
										uint8_t* q = &b.px[(((size_t)(y * cell + yy)) * b.w + x * cell + xx) * 4];
										q[0] = 0x14; q[1] = 0x18; q[2] = 0x21;
									}
					sp = Spr{ be->upload(b, false), 0, 0 };
					cache[key] = sp;
				}
				if (sp.tex)
				{
					float dx = std::round(X(56 + 107)) - sp.tex->w / 2.0f, dy = std::round(X(156 + 107)) - sp.tex->h / 2.0f;
					be->draw(sp.tex, std::round(dx), std::round(dy), (float)sp.tex->w, (float)sp.tex->h, Color(1, 1, 1, a));
				}
			}
		}
		text("Open in a browser", L, 96, 16, theme::muted, false, W, a, 1, true);
		float y = 122;
		if (st.urls.empty()) { text("Connect to Wi-Fi first", L, y, 22, theme::red, true, W, a); y += 30; }
		for (size_t i = 0; i < st.urls.size() && i < 2; i++)
		{
			std::string u = st.urls[i];
			if (u.rfind("http://", 0) == 0) u = u.substr(7);
			text(u, L, y, 22, theme::ink, true, W, a);
			y += 30;
		}
		fill(L, y + 8, W, 2, theme::line, 0.5f * a);
		text("PIN", L, y + 22, 16, theme::muted, false, 0, a, 1, true);
		text(st.pin, L, y + 44, 56, theme::accentBase, true, 0, a, 8);
		y += 124;
		for (auto& l : wrap("Same Wi-Fi as this handheld. The QR code fills in the PIN.", 18, W, 3))
		{
			text(l, L, y, 18, theme::muted, false, 0, a);
			y += 22;
		}
	}

	void drawToast(int screen)
	{
		if (toast.empty()) return;
		double t = nowMs() - toastAt;
		if (t > 3200) { toast.clear(); return; }
		float a = t < 150 ? (float)(t / 150) : (t > 2900 ? (float)((3200 - t) / 300) : 1);
		float w = std::min(560.0f, textW(toast, true, 18) + 40);
		BoxStyle b = style(12, 3, theme::edge, 0x10141c, { Band{ 0, 6, Color::hex(0x232b3a) } }, { Shadow{ 3, 3, 0, Color(0, 0, 0, 0.45f), false } });
		float y = screen == 1 ? 352 : 410;
		nine("toast", b, 13, 320 - w / 2, y, w, 38, a);
		text(toast, 320, y + 8, 18, theme::ink, true, w - 30, a, 0, false, true);
	}

	// what each screen gets over the engine's frame
	double overlayMs = 0;
	void overlay(int screen)
	{
		double t0 = nowMs();
		overlayDraw(screen);
		overlayMs += nowMs() - t0;
	}
	void overlayDraw(int screen)
	{
		hits[screen].clear();
		if (dual)
		{
			if (screen == 0)
			{
				if (mode == CONNECT) drawConnect();
				else if (mode == MENU && !menus.empty() && !menus.back().notes.empty()) drawNotes(menus.back().notes);
			}
			else
			{
				if (mode == MENU && !menus.empty())
				{
					fill(0, 0, 640, 480, 0x000000, 0.5f * overlayP());
					drawMenu(1, 66, 34, 508, 380);
				}
				drawToast(1);
			}
			return;
		}
		// one screen: the shelf (screen 1) or a port's page (screen 0)
		if (screen != shownScreen()) return;
		if (screen == 0)
		{
			if (mode == CONNECT) drawConnect();
			else if (mode == NOTES) drawNotes(notesTag);
			else if (mode == MENU && !menus.empty()) drawMenu(0, 26, 68, 290, 390);
		}
		drawToast(screen);
	}
	int shownScreen() const { return dual ? 1 : (mode == SHELF ? 1 : 0); }
};

// ---- the Source over the host's state ----------------------------------------------------------------------------
SysInfo Src::system(int)
{
	SysInfo si;
	si.id = "ports"; si.name = "Ports"; si.maker = "";
	si.accent = Color::hex(theme::accentBase);
	si.icon = h->assets + "/icons/ports.png";
	si.games = h->itemCount();
	return si;
}
int Src::gameCount() { return h->itemCount(); }
GameInfo Src::game(int i)
{
	GameInfo g;
	g.icon = h->assets + "/icons/ports.png";
	g.statLabels[0] = "Installed"; g.statLabels[1] = "Latest"; g.statLabels[2] = "Size";
	if (h->isConnect(i))
	{
		const State& st = h->st;
		g.title = "Connect";
		g.sub = st.webRunning && !st.urls.empty() ? st.urls[0].substr(st.urls[0].rfind("http://", 0) == 0 ? 7 : 0) : "Manage ports from a browser";
		g.box = h->assets + "/icons/connect-cover.png";
		g.icon = h->assets + "/icons/connect.png";
		g.statLabels[0] = "Web"; g.statLabels[1] = "PIN"; g.statLabels[2] = "Watched";
		g.plays = st.webRunning ? "On" : "Off";
		g.last = st.webRunning ? st.pin : "-";
		g.time = std::to_string(st.ports.size());
		g.hasAccent = true; g.accent = Color::hex(0x4a9bff);
		g.achLabel = st.webRunning ? "A shows the QR code" : (st.online ? "A starts the web interface" : "No Wi-Fi");
		g.achA = st.webRunning ? 1 : 0; g.achB = 1;
		return g;
	}
	const Port& p = h->st.ports[i];
	g.title = p.title;
	g.box = p.art;
	bool busy = p.jobState == "running" || p.jobState == "queued";
	if (busy || p.jobState == "failed") g.sub = p.jobMsg.empty() ? p.jobState : p.jobMsg;
	else if (p.status == "update") g.sub = "Update " + p.inst + " -> " + p.latest;
	else if (p.status == "unknown_version") g.sub = "Installed · latest " + p.latest;
	else if (p.status == "not_installed") g.sub = "Not installed · " + (p.latest.empty() ? std::string("no release") : p.latest);
	else if (p.status == "error") g.sub = p.error.empty() ? "Error" : p.error;
	else g.sub = "Up to date · " + p.inst;
	g.plays = p.installed ? (p.inst.empty() ? "?" : p.inst) : "-";
	g.last = p.latest.empty() ? "-" : p.latest;
	g.time = p.size > 0 ? humanSize(p.size) : "-";
	uint32_t tint = theme::portTints[std::hash<std::string>()(p.repo) % (sizeof theme::portTints / sizeof theme::portTints[0])];
	g.hasAccent = true; g.accent = Color::hex(tint);
	// the meter: what is going on with it
	if (busy || p.jobState == "failed")
	{
		g.achLabel = p.jobMsg.empty() ? p.jobState : p.jobMsg;
		g.achB = 100;
		g.achA = p.jobState == "failed" ? 100 : (p.jobProg >= 0 ? (int)std::lround(p.jobProg * 100) : 0);
	}
	else if (p.status == "update" || p.status == "unknown_version") { g.achLabel = "Update available"; g.achA = 100; g.achB = 100; }
	else if (p.origTotal > 0) { g.achLabel = "Original files: " + humanSize(p.origTotal) + " to free"; g.achA = 0; g.achB = 100; }
	else if (p.status == "current") { g.achLabel = "Up to date"; g.achA = 100; g.achB = 100; }
	return g;
}
float Src::battery()
{
	if (nowMs() - h->batteryAt > 30000) { h->batteryLevel = readBattery(h->batteryCharging); h->batteryAt = nowMs(); }
	return h->batteryLevel;
}
bool Src::charging() { battery(); return h->batteryCharging; }
bool Src::online()
{
	if (nowMs() - h->netAt > 5000) { h->net = hasNetwork(); h->netAt = nowMs(); }
	return h->net;
}
void Src::selectGame(int i) { h->sel = i; }
std::vector<std::pair<std::string, std::string>> Src::hints(bool library, int, int game)
{
	if (!library || h->mode != Host::SHELF) return {};
	std::vector<std::pair<std::string, std::string>> v = { { "X", "Check" } };
	const Port* p = h->port(game);
	if (p && !p->orig.empty()) v.push_back({ "Y", "Files" });
	return v;
}
bool Src::hostOpen(int game) { h->openItem(game); return true; }
std::string Src::plateLabel(int game)
{
	if (h->isConnect(game)) return h->st.webRunning ? "SHOW" : "OPEN";
	const Port* p = h->port(game);
	if (!p) return "";
	if (p->jobState == "running" || p->jobState == "queued") return "WAIT";
	if (p->status == "update" || p->status == "unknown_version") return "UPDATE";
	if (!p->installed) return "GET";
	return "OPEN";
}
bool Src::hostFaces(int, std::string& lk, std::string& ll, std::string& rk, std::string& rl, int& rkind)
{
	if (h->mode == Host::MENU) { lk = "B"; ll = "Back"; rk = "A"; rl = "OK"; rkind = 0; return true; }
	if (h->mode != Host::SHELF) { lk = "B"; ll = "Back"; rk = ""; rl = h->mode == Host::CONNECT ? "Web on" : "Notes"; rkind = 2; return true; }
	lk = "B"; ll = "Exit";
	if (h->st.busy) { rk = ""; rl = "Working…"; rkind = 2; }
	else if (h->st.updates > 0) { rk = "START"; rl = "Update all"; rkind = 0; }
	else { rk = ""; rl = "Up to date"; rkind = 2; }
	return true;
}
bool Src::hostFace(const std::string& key)
{
	if (h->mode != Host::SHELF) { h->button(key == "A" ? "a" : "b"); return true; }
	if (key == "B") h->tryQuit();
	else if (key == "X") h->checkAll();
	else if (key == "Y") h->filesOf(h->ui->gameIndex());
	else if (key == "START") h->updateAll();
	return true;
}

// ---- the display: SDL windows showing the composed screens ------------------------------------------------------
struct Window
{
	SDL_Window* win = nullptr;
	SDL_Renderer* ren = nullptr;
	SDL_Texture* tex = nullptr;
	int W = 0, H = 0;
	int screen = 0;			// which composed screen it shows
	int shown = -1;			// the screen its texture holds now (another one: a full upload)
	SDL_Rect dst{};
};

static bool openWindow(Window& w, int display, int bw, int bh, bool windowed)
{
	SDL_Rect b;
	SDL_GetDisplayBounds(display, &b);
	Uint32 flags = SDL_WINDOW_SHOWN | (windowed ? 0 : SDL_WINDOW_FULLSCREEN_DESKTOP);
	w.win = SDL_CreateWindow("Slopetanium", windowed ? SDL_WINDOWPOS_CENTERED_DISPLAY(display) : b.x,
		windowed ? SDL_WINDOWPOS_CENTERED_DISPLAY(display) : b.y, windowed ? bw : b.w, windowed ? bh : b.h, flags);
	if (!w.win) { logf("window: %s", SDL_GetError()); return false; }
	w.ren = SDL_CreateRenderer(w.win, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC);
	if (!w.ren) w.ren = SDL_CreateRenderer(w.win, -1, SDL_RENDERER_SOFTWARE);
	if (!w.ren) { logf("renderer: %s", SDL_GetError()); return false; }
	SDL_GetRendererOutputSize(w.ren, &w.W, &w.H);
	w.tex = SDL_CreateTexture(w.ren, SDL_PIXELFORMAT_ARGB8888, SDL_TEXTUREACCESS_STREAMING, bw, bh);
	if (!w.tex) { logf("texture: %s", SDL_GetError()); return false; }
	w.dst = SDL_Rect{ (w.W - bw) / 2, (w.H - bh) / 2, bw, bh };
	SDL_RendererInfo ri;
	SDL_GetRendererInfo(w.ren, &ri);
	logf("display %d: %dx%d, screen %dx%d, renderer %s%s", display, w.W, w.H, bw, bh, ri.name,
		(ri.flags & SDL_RENDERER_PRESENTVSYNC) ? " (vsync)" : "");
	return true;
}

// copies what changed into the window's texture and shows it; false when there was nothing to show
static bool present(Window& w, banana::SoftBackend& be, const banana::Damage& dmg, int screen)
{
	int bw = be.width(), bh = be.height();
	banana::Damage d = dmg;
	if (w.shown != screen) { d = banana::Damage{ 0, 0, bw, bh }; w.shown = screen; }
	if (d.empty()) return false;
	SDL_Rect r{ d.x0, d.y0, d.x1 - d.x0, d.y1 - d.y0 };
	const uint32_t* px = be.pixels(screen) + (size_t)d.y0 * bw + d.x0;
	SDL_UpdateTexture(w.tex, &r, px, bw * 4);
	SDL_SetRenderDrawColor(w.ren, (theme::screenBg >> 16) & 255, (theme::screenBg >> 8) & 255, theme::screenBg & 255, 255);
	SDL_RenderClear(w.ren);
	SDL_RenderCopy(w.ren, w.tex, nullptr, &w.dst);
	SDL_RenderPresent(w.ren);
	return true;
}

// the bottom panel's touchscreen, by the device behind SDL's touch id (evdev: the id is its fd): its sysfs path
// names the bus address (the RG DS's bottom one is on fe5e0000.i2c)
static std::string touchPath(SDL_TouchID id)
{
	char link[64], dev[256] = { 0 }, sys[512] = { 0 };
	snprintf(link, sizeof link, "/proc/self/fd/%lld", (long long)id);
	ssize_t n = readlink(link, dev, sizeof dev - 1);
	if (n <= 0) return "";
	dev[n] = 0;
	const char* base = strrchr(dev, '/');
	if (!base || strncmp(base + 1, "event", 5) != 0) return "";
	std::string s = std::string("/sys/class/input/") + (base + 1) + "/device";
	char real[512];
	if (!realpath(s.c_str(), real)) return "";
	snprintf(sys, sizeof sys, "%s", real);
	return sys;
}

// ---- headless pictures (for the README and tests): both screens as PNG ------------------------------------------
static void writeScreen(banana::SoftBackend& be, int screen, const std::string& path)
{
	int w = be.width(), h = be.height();
	std::vector<uint8_t> rgb((size_t)w * h * 3);
	const uint32_t* px = be.pixels(screen);
	for (size_t i = 0; i < (size_t)w * h; i++) { rgb[i * 3] = (px[i] >> 16) & 255; rgb[i * 3 + 1] = (px[i] >> 8) & 255; rgb[i * 3 + 2] = px[i] & 255; }
	stbi_write_png(path.c_str(), w, h, 3, rgb.data(), w * 3);
}

int main(int argc, char** argv)
{
	std::string assets = ".", conf = ".", portFile, token = getenv("SLOP_LOCAL_TOKEN") ? getenv("SLOP_LOCAL_TOKEN") : "";
	std::string shot, script, touchBottom = getenv("SLOP_TOUCH_BOTTOM") ? getenv("SLOP_TOUCH_BOTTOM") : "fe5e0000.i2c";
	int screens = 0;	// 0 auto
	float forceScale = 0;
	bool windowed = false, stats = getenv("SLOP_FPS") != nullptr;
	for (int i = 1; i < argc; i++)
	{
		std::string a = argv[i];
		auto next = [&]() -> std::string { return i + 1 < argc ? argv[++i] : ""; };
		if (a == "--assets") assets = next();
		else if (a == "--conf") conf = next();
		else if (a == "--port-file") portFile = next();
		else if (a == "--token") token = next();
		else if (a == "--screens") screens = atoi(next().c_str());
		else if (a == "--scale") forceScale = (float)atof(next().c_str());
		else if (a == "--windowed") windowed = true;
		else if (a == "--shot") shot = next();
		else if (a == "--script") script = next();
		else { logf("unknown option %s", a.c_str()); return 2; }
	}
	if (portFile.empty()) portFile = conf + "/core.port";
	setDecoder(stbDecode);
	setSizer(stbSize);
	theme::apply();

	bool headless = !shot.empty();
	// headless pictures need no video at all (some firmware's SDL has no dummy driver)
	if (SDL_Init(headless ? SDL_INIT_EVENTS : (SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER | SDL_INIT_JOYSTICK | SDL_INIT_EVENTS)) != 0)
	{
		logf("SDL_Init: %s", SDL_GetError());
		return 1;
	}
	Host host;
	host.assets = assets;
	host.conf = conf;
	host.src.h = &host;
	host.wakeEvent = SDL_RegisterEvents(1);

	int ndisp = headless ? 1 : SDL_GetNumVideoDisplays();
	host.dual = screens == 2 || (screens == 0 && ndisp >= 2);
	// the scale: the engine's CSS px grid (640x480) to the display, letterboxed
	SDL_Rect b0{ 0, 0, 640, 480 };
	if (!headless) SDL_GetDisplayBounds(0, &b0);
	float s = forceScale > 0 ? forceScale : std::min(b0.w / 640.0f, b0.h / 480.0f);
	if (windowed && forceScale <= 0) s = 1;
	s = std::max(0.5f, std::floor(s * 20 + 0.001f) / 20);	// a tidy step: whole CSS px land on whole device px more often
	host.s = s;
	int bw = (int)std::lround(640 * s), bh = (int)std::lround(480 * s);
	themeFiles.wallTop = theme::wall(conf, bw, bh, s, true);
	themeFiles.wallBottom = theme::wall(conf, bw, bh, s, false);
	host.R = Font::load(assets + "/fonts/PixelifySans-Regular.ttf");
	host.M = Font::load(assets + "/fonts/PixelifySans-Medium.ttf");
	if (!host.R || !host.M) { logf("fonts missing in %s/fonts", assets.c_str()); return 1; }

	host.be.reset(new banana::SoftBackend(bw, bh));
	host.be->overlay = [&host](int screen) { host.overlay(screen); };
	host.ui.reset(new UI(&host.src, host.be.get(), assets, s, "dark"));
	host.link.reset(new slop::CoreLink(portFile, token, [&host] { host.wake(); }));

	// the picture workers: one fewer than the cores, at most three, niced (the frames' thread never waits for them)
	std::atomic<bool> stopWork{ false };
	std::mutex wm;
	std::condition_variable wcv;
	uint64_t kicks = 0;
	host.ui->setNotifiers([&] { { std::lock_guard<std::mutex> l(wm); kicks++; } wcv.notify_all(); }, [&host] { host.wake(); });
	std::vector<std::thread> workers;
	int nw = std::max(1, std::min(3, (int)std::thread::hardware_concurrency() - 1));
	for (int i = 0; i < nw; i++)
		workers.emplace_back([&] {
			setpriority(PRIO_PROCESS, (id_t)syscall(SYS_gettid), 5);
			uint64_t seen = 0;
			for (;;)
			{
				{
					std::unique_lock<std::mutex> l(wm);
					wcv.wait(l, [&] { return stopWork.load() || kicks != seen; });
					if (stopWork) return;
					seen = kicks;
				}
				while (!stopWork && host.ui->workOnce()) {}
			}
		});

	host.ui->setLibrary(0, 0);

	// ---- headless: wait for the core, run the script, write the screens ----
	if (headless)
	{
		double t0 = nowMs();
		std::string json;
		uint64_t serial = 0;
		while (nowMs() - t0 < 8000 && !host.link->state(json, serial)) SDL_Delay(50);
		if (!json.empty()) host.apply(json);
		auto settle = [&](int ms) {
			double until = nowMs() + ms;
			while (nowMs() < until)
			{
				std::string j;
				if (host.link->state(j, serial) && !j.empty()) host.apply(j);
				host.ui->update(nowMs()); host.ui->finishWork(); host.ui->update(nowMs()); host.ui->render();
				SDL_Delay(16);
			}
		};
		settle(600);
		std::stringstream ss(script);
		std::string step;
		while (std::getline(ss, step, ','))
		{
			if (step.empty()) continue;
			if (step.rfind("wait", 0) == 0) { settle(atoi(step.c_str() + 4)); continue; }
			host.button(step);
			settle(500);
		}
		settle(700);
		writeScreen(*host.be, 0, shot + "-top.png");
		writeScreen(*host.be, 1, shot + "-bottom.png");
		logf("wrote %s-top.png and %s-bottom.png (dual layout %d)", shot.c_str(), shot.c_str(), host.dual);
		stopWork = true; wcv.notify_all();
		for (auto& t : workers) t.join();
		host.link.reset();
		SDL_Quit();
		return 0;
	}

	// ---- the windows ----
	std::vector<Window> wins(host.dual ? 2 : 1);
	if (host.dual)
	{
		if (!openWindow(wins[0], 0, bw, bh, windowed) || !openWindow(wins[1], ndisp >= 2 ? 1 : 0, bw, bh, windowed)) return 1;
		wins[0].screen = 0; wins[1].screen = 1;
	}
	else
	{
		if (!openWindow(wins[0], 0, bw, bh, windowed)) return 1;
		wins[0].screen = 1;
	}
	SDL_ShowCursor(SDL_DISABLE);
	std::vector<SDL_GameController*> pads;
	for (int i = 0; i < SDL_NumJoysticks(); i++) if (SDL_IsGameController(i)) if (auto* p = SDL_GameControllerOpen(i)) pads.push_back(p);

	std::map<SDL_TouchID, int> touchScreen;	// which composed screen a touch device sits on
	auto screenOfTouch = [&](SDL_TouchID id) -> int {
		auto it = touchScreen.find(id);
		if (it != touchScreen.end()) return it->second;
		int scr = host.shownScreen();
		if (host.dual)
		{
			std::string p = touchPath(id);
			int n = SDL_GetNumTouchDevices();
			bool anyBottom = false;
			for (int i = 0; i < n; i++) if (touchPath(SDL_GetTouchDevice(i)).find(touchBottom) != std::string::npos) anyBottom = true;
			scr = !anyBottom || p.find(touchBottom) != std::string::npos ? 1 : 0;
			logf("touch device %lld: %s -> %s screen", (long long)id, p.c_str(), scr == 1 ? "bottom" : "top");
		}
		touchScreen[id] = scr;
		return scr;
	};

	std::string json;
	uint64_t serial = 0;
	std::string held;
	double repeatAt = 0;
	bool selectHeld = false, startHeld = false;
	// frame statistics (SLOP_FPS=1): frames, presents, the longest frame, every 2 s in the log
	double stT = nowMs(), stMax = 0;
	int stFrames = 0, stPresents = 0;

	auto padButton = [](uint8_t b) -> std::string {
		switch (b)
		{
		case SDL_CONTROLLER_BUTTON_DPAD_LEFT: return "left";
		case SDL_CONTROLLER_BUTTON_DPAD_RIGHT: return "right";
		case SDL_CONTROLLER_BUTTON_DPAD_UP: return "up";
		case SDL_CONTROLLER_BUTTON_DPAD_DOWN: return "down";
		case SDL_CONTROLLER_BUTTON_A: return "a";
		case SDL_CONTROLLER_BUTTON_B: return "b";
		case SDL_CONTROLLER_BUTTON_X: return "x";
		case SDL_CONTROLLER_BUTTON_Y: return "y";
		case SDL_CONTROLLER_BUTTON_LEFTSHOULDER: return "l";
		case SDL_CONTROLLER_BUTTON_RIGHTSHOULDER: return "r";
		case SDL_CONTROLLER_BUTTON_START: return "start";
		case SDL_CONTROLLER_BUTTON_BACK: return "select";
		case SDL_CONTROLLER_BUTTON_GUIDE: return "guide";
		}
		return "";
	};
	auto keyButton = [](SDL_Keycode k) -> std::string {
		switch (k)
		{
		case SDLK_LEFT: return "left";
		case SDLK_RIGHT: return "right";
		case SDLK_UP: return "up";
		case SDLK_DOWN: return "down";
		case SDLK_RETURN: case SDLK_a: return "a";
		case SDLK_ESCAPE: case SDLK_BACKSPACE: case SDLK_b: return "b";
		case SDLK_x: return "x";
		case SDLK_y: return "y";
		case SDLK_PAGEUP: return "l";
		case SDLK_PAGEDOWN: return "r";
		case SDLK_s: return "start";
		}
		return "";
	};
	int axisX = 0, axisY = 0;
	auto press = [&](const std::string& b) {
		if (b.empty()) return;
		if (b == "guide" || (b == "start" && selectHeld) || (b == "select" && startHeld)) { host.tryQuit(); return; }
		if (b == "select") return;
		host.button(b);
	};

	// on-device tests: SLOP_SCRIPT="wait800,right,wait300,a" presses buttons on a timeline; SLOP_SCREENSHOT=/tmp/x
	// writes x-top.png and x-bottom.png after SLOP_SCREENSHOT_AFTER ms (default 4000) and quits
	std::vector<std::pair<double, std::string>> timeline;
	{
		double at = nowMs() + 1500;
		std::stringstream ss(getenv("SLOP_SCRIPT") ? getenv("SLOP_SCRIPT") : "");
		std::string step;
		while (std::getline(ss, step, ','))
		{
			if (step.rfind("wait", 0) == 0) at += atof(step.c_str() + 4);
			else if (!step.empty()) { timeline.push_back({ at, step }); at += 60; }
		}
	}
	const char* shotEnv = getenv("SLOP_SCREENSHOT");
	double shotAt = nowMs() + (getenv("SLOP_SCREENSHOT_AFTER") ? atof(getenv("SLOP_SCREENSHOT_AFTER")) : 4000);

	while (!host.quit)
	{
		double t0 = nowMs();
		while (!timeline.empty() && t0 >= timeline.front().first) { press(timeline.front().second); timeline.erase(timeline.begin()); }
		if (shotEnv && t0 >= shotAt)
		{
			writeScreen(*host.be, 0, std::string(shotEnv) + "-top.png");
			writeScreen(*host.be, 1, std::string(shotEnv) + "-bottom.png");
			logf("screens written to %s-*.png", shotEnv);
			break;
		}
		int wakeMs = host.ui->nextWake();
		if (host.overlayMoving() || !held.empty()) wakeMs = wakeMs < 0 ? 16 : std::min(wakeMs, 16);
		int timeout = wakeMs < 0 ? 1000 : wakeMs;
		if (!timeline.empty()) timeout = std::min(timeout, std::max(0, (int)(timeline.front().first - t0)));
		if (shotEnv) timeout = std::min(timeout, std::max(0, (int)(shotAt - t0)));
		SDL_Event ev;
		bool got = timeout == 0 ? SDL_PollEvent(&ev) : SDL_WaitEventTimeout(&ev, timeout);
		while (got)
		{
			switch (ev.type)
			{
			case SDL_QUIT: host.quit = true; break;
			case SDL_KEYDOWN:
				if (!ev.key.repeat) { std::string b = keyButton(ev.key.keysym.sym); press(b); if (b == "left" || b == "right" || b == "up" || b == "down") { held = b; repeatAt = nowMs() + 380; } }
				break;
			case SDL_KEYUP: if (keyButton(ev.key.keysym.sym) == held) held.clear(); break;
			case SDL_CONTROLLERBUTTONDOWN:
			{
				std::string b = padButton(ev.cbutton.button);
				if (b == "select") selectHeld = true;
				if (b == "start") startHeld = true;
				press(b);
				if (b == "left" || b == "right" || b == "up" || b == "down") { held = b; repeatAt = nowMs() + 380; }
				break;
			}
			case SDL_CONTROLLERBUTTONUP:
			{
				std::string b = padButton(ev.cbutton.button);
				if (b == "select") selectHeld = false;
				if (b == "start") startHeld = false;
				if (b == held) held.clear();
				break;
			}
			case SDL_CONTROLLERAXISMOTION:
				if (ev.caxis.axis == SDL_CONTROLLER_AXIS_LEFTX || ev.caxis.axis == SDL_CONTROLLER_AXIS_LEFTY)
				{
					int v = ev.caxis.value < -16000 ? -1 : (ev.caxis.value > 16000 ? 1 : 0);
					int& cur = ev.caxis.axis == SDL_CONTROLLER_AXIS_LEFTX ? axisX : axisY;
					if (v != cur)
					{
						cur = v;
						std::string b = v == 0 ? "" : ev.caxis.axis == SDL_CONTROLLER_AXIS_LEFTX ? (v < 0 ? "left" : "right") : (v < 0 ? "up" : "down");
						if (!b.empty()) { press(b); held = b; repeatAt = nowMs() + 380; }
						else held.clear();
					}
				}
				break;
			case SDL_CONTROLLERDEVICEADDED:
				if (SDL_IsGameController(ev.cdevice.which)) if (auto* p = SDL_GameControllerOpen(ev.cdevice.which)) pads.push_back(p);
				break;
			case SDL_FINGERDOWN: case SDL_FINGERUP: case SDL_FINGERMOTION:
			{
				int scr = screenOfTouch(ev.tfinger.touchId);
				Window& w = wins[host.dual ? scr : 0];
				if (!host.dual) scr = host.shownScreen();
				// the finger in the composed screen's device px (the touch spans the display; the screen is centred in it)
				float px = ev.tfinger.x * w.W - w.dst.x, py = ev.tfinger.y * w.H - w.dst.y;
				float cx = px / host.s, cy = py / host.s;
				bool overlayOwns = host.mode != Host::SHELF && (host.dual ? (host.mode == Host::MENU ? scr == 1 : true) : true);
				if (overlayOwns)
				{
					if (ev.type == SDL_FINGERDOWN)
					{
						bool hit = false;
						auto hs = host.hits[scr];
						for (auto it = hs.rbegin(); it != hs.rend(); ++it)
							if (cx >= it->x && cx < it->x + it->w && cy >= it->y && cy < it->y + it->h) { it->fn(); hit = true; break; }
						if (!hit && (host.mode == Host::CONNECT || host.mode == Host::NOTES || scr == host.shownScreen())) host.button("b");
					}
				}
				else if (scr == 1)
				{
					if (ev.type == SDL_FINGERDOWN) host.ui->touch(1, px, py, true);
					else if (ev.type == SDL_FINGERUP) host.ui->touch(1, px, py, false);
					else host.ui->touchMove(1, px, py);
					host.sel = host.ui->gameIndex();
				}
				break;
			}
			default:
				break;
			}
			got = SDL_PollEvent(&ev);
		}
		if (!held.empty() && nowMs() >= repeatAt) { host.button(held); repeatAt = nowMs() + 110; }
		if (host.link->state(json, serial) && !json.empty()) { host.apply(json); json.clear(); }

		double now = nowMs();
		host.ui->update(now);
		double tU = nowMs();
		host.overlayMs = 0;
		host.ui->render();
		double tR = nowMs();
		banana::Damage dmg[2] = { host.be->takeDamage(0), host.be->takeDamage(1) };
		int presented = 0;
		for (auto& w : wins)
		{
			int scr = host.dual ? w.screen : host.shownScreen();
			if (present(w, *host.be, dmg[scr], scr)) presented++;
		}
		double frameMs = nowMs() - now;	// (the work, not the wait for input before it)
		if (stats && frameMs > 25)
			logf("slow frame: events+apply %.1f, update %.1f, render %.1f (overlay %.1f), present %.1f ms; mode %d sel %d",
				now - t0, tU - now, tR - tU, host.overlayMs, nowMs() - tR, (int)host.mode, host.sel);
		// moving but nothing to show (a screen not on display): keep the frame rhythm without a vsync to wait on
		if (presented == 0 && (wakeMs == 0 || host.overlayMoving())) SDL_Delay(12);
		if (stats)
		{
			stFrames++; stPresents += presented; stMax = std::max(stMax, frameMs);
			if (nowMs() - stT >= 2000)
			{
				logf("frames %d, presents %d in %.0f ms, longest %.1f ms", stFrames, stPresents, nowMs() - stT, stMax);
				stT = nowMs(); stFrames = stPresents = 0; stMax = 0;
			}
		}
	}

	stopWork = true;
	wcv.notify_all();
	for (auto& t : workers) t.join();
	host.link.reset();
	for (auto* p : pads) SDL_GameControllerClose(p);
	host.ui.reset();
	for (auto& w : wins) { if (w.tex) SDL_DestroyTexture(w.tex); if (w.ren) SDL_DestroyRenderer(w.ren); if (w.win) SDL_DestroyWindow(w.win); }
	SDL_Quit();
	return 0;
}
