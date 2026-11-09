#include "net.h"
#include <arpa/inet.h>
#include <chrono>
#include <cstring>
#include <fstream>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

namespace slop
{
	int httpLocal(int port, const std::string& method, const std::string& path, const std::string& token,
		const std::string& body, std::string& out)
	{
		out.clear();
		int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
		if (fd < 0) return 0;
		timeval tv{ 5, 0 };
		setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
		setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
		sockaddr_in a{};
		a.sin_family = AF_INET;
		a.sin_port = htons((uint16_t)port);
		a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
		if (connect(fd, (sockaddr*)&a, sizeof a) != 0) { close(fd); return 0; }
		std::string req = method + " " + path + " HTTP/1.0\r\nHost: 127.0.0.1\r\nX-Local-Token: " + token +
			"\r\nContent-Type: application/json\r\nContent-Length: " + std::to_string(body.size()) + "\r\n\r\n" + body;
		size_t sent = 0;
		while (sent < req.size())
		{
			ssize_t n = send(fd, req.data() + sent, req.size() - sent, MSG_NOSIGNAL);
			if (n <= 0) { close(fd); return 0; }
			sent += (size_t)n;
		}
		std::string resp;
		char buf[16384];
		for (;;)
		{
			ssize_t n = recv(fd, buf, sizeof buf, 0);
			if (n <= 0) break;
			resp.append(buf, (size_t)n);
		}
		close(fd);
		size_t he = resp.find("\r\n\r\n");
		if (resp.compare(0, 5, "HTTP/") != 0 || he == std::string::npos) return 0;
		int status = atoi(resp.c_str() + resp.find(' ') + 1);
		out = resp.substr(he + 4);
		return status;
	}

	CoreLink::CoreLink(const std::string& pf, const std::string& tok, std::function<void()> w) : portFile(pf), token(tok), wake(w)
	{
		th = std::thread([this] { run(); });
	}

	CoreLink::~CoreLink()
	{
		{ std::lock_guard<std::mutex> l(m); stop = true; }
		cv.notify_all();
		if (th.joinable()) th.join();
	}

	bool CoreLink::state(std::string& json, uint64_t& s)
	{
		std::lock_guard<std::mutex> l(m);
		if (serial == 0) return false;
		if (s != serial) { json = latest; s = serial; }
		return true;
	}

	void CoreLink::post(const std::string& path, const std::string& body)
	{
		{ std::lock_guard<std::mutex> l(m); queue.push_back(Req{ "POST", path, body, nullptr }); kick = true; }
		cv.notify_all();
	}

	void CoreLink::get(const std::string& path, std::function<void(int, const std::string&)> done)
	{
		{ std::lock_guard<std::mutex> l(m); queue.push_back(Req{ "GET", path, "", done }); }
		cv.notify_all();
	}

	void CoreLink::call(const std::string& method, const std::string& path, const std::string& body,
		std::function<void(int, const std::string&)> done)
	{
		{ std::lock_guard<std::mutex> l(m); queue.push_back(Req{ method, path, body, done }); }
		cv.notify_all();
	}

	void CoreLink::refresh()
	{
		{ std::lock_guard<std::mutex> l(m); kick = true; }
		cv.notify_all();
	}

	void CoreLink::run()
	{
		using clock = std::chrono::steady_clock;
		auto next = clock::now();
		for (;;)
		{
			Req r;
			bool haveReq = false, poll = false;
			{
				std::unique_lock<std::mutex> l(m);
				cv.wait_until(l, next, [&] { return stop || kick || !queue.empty(); });
				if (stop) return;
				if (!queue.empty()) { r = queue.front(); queue.pop_front(); haveReq = true; }
				else { poll = true; kick = false; }
			}
			if (port <= 0)
			{
				std::ifstream f(portFile);
				int p = 0;
				if (f >> p && p > 0) port = p;
				if (port <= 0) { next = clock::now() + std::chrono::milliseconds(150); if (haveReq) { std::lock_guard<std::mutex> l(m); queue.push_front(r); } continue; }
			}
			if (haveReq)
			{
				std::string out;
				int st = httpLocal(port, r.method, r.path, token, r.body, out);
				if (r.done) r.done(st, out);
				if (r.method == "POST") { std::lock_guard<std::mutex> l(m); kick = true; }
				continue;
			}
			if (poll)
			{
				std::string out;
				int st = httpLocal(port, "GET", "/api/state", token, "", out);
				lastOk = st == 200;
				if (st == 200)
				{
					bool changed;
					{
						std::lock_guard<std::mutex> l(m);
						changed = out != latest;
						if (changed) { latest.swap(out); serial++; }
					}
					if (changed && wake) wake();
				}
				else
				{
					lastError = st == 0 ? "no answer from the core" : "HTTP " + std::to_string(st);
					// the core went away (a new one may write another port)
					if (st == 0) { std::ifstream f(portFile); int p = 0; if (f >> p && p > 0) port = p; }
				}
				next = clock::now() + std::chrono::milliseconds(700);
			}
		}
	}
}
