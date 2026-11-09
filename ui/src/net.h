#pragma once
// Slopetanium UI: the link to the Python core (its API on 127.0.0.1). One thread does every request, so the frames
// never wait for the network: the latest state comes back as JSON text, actions are queued.
#include <atomic>
#include <condition_variable>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

namespace slop
{
	// one HTTP/1.0 request to 127.0.0.1:port; returns the status (0: no connection) and the body
	int httpLocal(int port, const std::string& method, const std::string& path, const std::string& token,
		const std::string& body, std::string& out);

	class CoreLink
	{
	public:
		// portFile: where the core writes its port (it may come a moment after we start)
		CoreLink(const std::string& portFile, const std::string& token, std::function<void()> wake);
		~CoreLink();
		// the newest state (and its serial, bumped when the text changed); false before the first one
		bool state(std::string& json, uint64_t& serial);
		void post(const std::string& path, const std::string& body = "{}");	// fire and forget, then a fresh state
		// a GET answered on the link's thread; done(status, body) runs there too (keep it short, wake the main loop)
		void get(const std::string& path, std::function<void(int, const std::string&)> done);
		// any request with its answer (done runs on the link's thread)
		void call(const std::string& method, const std::string& path, const std::string& body,
			std::function<void(int, const std::string&)> done);
		void refresh();			// fetch the state now
		bool connected() const { return port > 0 && lastOk; }
		std::string lastError;

	private:
		void run();
		std::string portFile, token;
		std::function<void()> wake;
		std::thread th;
		std::mutex m;
		std::condition_variable cv;
		bool stop = false, kick = false;
		struct Req { std::string method, path, body; std::function<void(int, const std::string&)> done; };
		std::deque<Req> queue;
		std::string latest;
		uint64_t serial = 0;
		std::atomic<int> port{ 0 };
		std::atomic<bool> lastOk{ false };
	};
}
