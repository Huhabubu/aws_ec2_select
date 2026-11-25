#include "net.hpp"
#include "stats.hpp"

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;

enum class Kind { RestRtt, WsApiRtt, StreamLag };

struct Target {
  std::string name;
  Kind kind;
  std::string host;
  std::uint16_t port;
  std::string path;
  std::string request;
};

struct Options {
  std::string market;
  int samples = 200;
  int warmup = 20;
  int connect_samples = 5;
  int interval_ms = 20;
  int timeout_ms = 3000;
  int stream_seconds = 10;
  std::string label;
  std::string out;
  std::vector<std::string> only;
  std::vector<std::string> sets;
};

struct IpResult {
  std::string ip;
  std::vector<double> tcp_us;
  std::vector<double> tls_us;
  std::vector<double> rtt_us;
  std::vector<double> lag_us;
  int errors = 0;
  std::string first_error;
};

struct TargetResult {
  Target target;
  std::string resolve_error;
  std::vector<IpResult> ips;
};

std::vector<Target> futures_targets() {
  return {
      {"rest", Kind::RestRtt, "fapi.binance.com", 443, "/fapi/v1/time", ""},
      {"ws_api", Kind::WsApiRtt, "ws-fapi.binance.com", 443, "/ws-fapi/v1",
       R"("method":"ticker.book","params":{"symbol":"BTCUSDT"})"},
      {"stream", Kind::StreamLag, "fstream.binance.com", 443, "/ws/btcusdt@bookTicker", ""},
  };
}

std::vector<Target> spot_targets() {
  return {
      {"rest", Kind::RestRtt, "api.binance.com", 443, "/api/v3/time", ""},
      {"ws_api", Kind::WsApiRtt, "ws-api.binance.com", 443, "/ws-api/v3", R"("method":"time")"},
      {"stream", Kind::StreamLag, "stream.binance.com", 9443, "/ws/btcusdt@trade", ""},
  };
}

const char* kind_name(Kind kind) {
  switch (kind) {
    case Kind::RestRtt:
      return "rest_rtt";
    case Kind::WsApiRtt:
      return "ws_api_rtt";
    case Kind::StreamLag:
      return "stream_lag";
  }
  return "unknown";
}

void usage() {
  std::fprintf(stderr,
               "usage: latency_probe --market futures|spot [--samples N] [--warmup N]\n"
               "       [--connect-samples N] [--interval-ms N] [--timeout-ms N]\n"
               "       [--stream-seconds N] [--only rest,ws_api,stream]\n"
               "       [--set target.field=value] [--label TEXT] [--out FILE]\n"
               "fields for --set: host, port, path, request\n");
}

bool parse_int(const std::string& text, int& value) {
  char* end = nullptr;
  const long parsed = std::strtol(text.c_str(), &end, 10);
  if (text.empty() || *end != '\0' || parsed < 0 || parsed > 10000000) {
    return false;
  }
  value = static_cast<int>(parsed);
  return true;
}

std::vector<std::string> split(const std::string& text, char separator) {
  std::vector<std::string> parts;
  std::stringstream stream(text);
  std::string part;
  while (std::getline(stream, part, separator)) {
    if (!part.empty()) {
      parts.push_back(part);
    }
  }
  return parts;
}

bool apply_set(std::vector<Target>& targets, const std::string& spec, std::string& error) {
  const std::size_t dot = spec.find('.');
  const std::size_t equals = spec.find('=');
  if (dot == std::string::npos || equals == std::string::npos || equals < dot) {
    error = "bad --set value: " + spec;
    return false;
  }
  const std::string name = spec.substr(0, dot);
  const std::string field = spec.substr(dot + 1, equals - dot - 1);
  const std::string value = spec.substr(equals + 1);
  for (Target& target : targets) {
    if (target.name != name) {
      continue;
    }
    if (field == "host") {
      target.host = value;
    } else if (field == "path") {
      target.path = value;
    } else if (field == "request") {
      target.request = value;
    } else if (field == "port") {
      int port = 0;
      if (!parse_int(value, port) || port < 1 || port > 65535) {
        error = "bad port: " + value;
        return false;
      }
      target.port = static_cast<std::uint16_t>(port);
    } else {
      error = "unknown field: " + field;
      return false;
    }
    return true;
  }
  error = "unknown target: " + name;
  return false;
}

bool parse_args(int argc, char** argv, Options& options) {
  for (int index = 1; index < argc; ++index) {
    const std::string arg = argv[index];
    if (arg == "-h" || arg == "--help") {
      return false;
    }
    if (index + 1 >= argc) {
      std::fprintf(stderr, "missing value for %s\n", arg.c_str());
      return false;
    }
    const std::string value = argv[++index];
    bool ok = true;
    if (arg == "--market") {
      options.market = value;
    } else if (arg == "--samples") {
      ok = parse_int(value, options.samples) && options.samples > 0;
    } else if (arg == "--warmup") {
      ok = parse_int(value, options.warmup);
    } else if (arg == "--connect-samples") {
      ok = parse_int(value, options.connect_samples);
    } else if (arg == "--interval-ms") {
      ok = parse_int(value, options.interval_ms);
    } else if (arg == "--timeout-ms") {
      ok = parse_int(value, options.timeout_ms) && options.timeout_ms > 0;
    } else if (arg == "--stream-seconds") {
      ok = parse_int(value, options.stream_seconds);
    } else if (arg == "--only") {
      options.only = split(value, ',');
    } else if (arg == "--set") {
      options.sets.push_back(value);
    } else if (arg == "--label") {
      options.label = value;
    } else if (arg == "--out") {
      options.out = value;
    } else {
      std::fprintf(stderr, "unknown option %s\n", arg.c_str());
      return false;
    }
    if (!ok) {
      std::fprintf(stderr, "bad value for %s: %s\n", arg.c_str(), value.c_str());
      return false;
    }
  }
  if (options.market != "futures" && options.market != "spot") {
    std::fprintf(stderr, "--market must be futures or spot\n");
    return false;
  }
  return true;
}

double since_us(Clock::time_point start) {
  return std::chrono::duration<double, std::micro>(Clock::now() - start).count();
}

void sleep_ms(int milliseconds) {
  if (milliseconds > 0) {
    std::this_thread::sleep_for(std::chrono::milliseconds(milliseconds));
  }
}

void note_error(IpResult& result, const std::string& error) {
  ++result.errors;
  if (result.first_error.empty()) {
    result.first_error = error;
  }
}

bool open_session(SSL_CTX* ctx, const Target& target, const std::string& ip,
                  const Options& options, probe::TlsConnection& conn, std::string& error) {
  double tcp_us = 0.0;
  double tls_us = 0.0;
  if (!conn.open(ctx, ip, target.port, target.host, options.timeout_ms, tcp_us, tls_us, error)) {
    return false;
  }
  if (target.kind == Kind::RestRtt) {
    return true;
  }
  return probe::ws_handshake(conn, target.host, target.path, error);
}

void measure_connect(SSL_CTX* ctx, const Target& target, const Options& options,
                     IpResult& result) {
  for (int index = 0; index < options.connect_samples; ++index) {
    probe::TlsConnection conn;
    double tcp_us = 0.0;
    double tls_us = 0.0;
    std::string error;
    if (conn.open(ctx, result.ip, target.port, target.host, options.timeout_ms, tcp_us, tls_us,
                  error)) {
      result.tcp_us.push_back(tcp_us);
      result.tls_us.push_back(tls_us);
    } else {
      note_error(result, error);
    }
    sleep_ms(options.interval_ms);
  }
}

void measure_rtt(SSL_CTX* ctx, const Target& target, const Options& options, IpResult& result) {
  probe::TlsConnection conn;
  std::string error;
  const int max_errors = 3 + options.samples / 10;
  const int total = options.warmup + options.samples;
  bool connected = false;
  int done = 0;
  long request_id = 0;
  while (done < total && result.errors < max_errors) {
    if (!connected) {
      if (!open_session(ctx, target, result.ip, options, conn, error)) {
        note_error(result, error);
        sleep_ms(200);
        continue;
      }
      connected = true;
    }
    std::string request;
    if (target.kind == Kind::RestRtt) {
      request = probe::build_http_get(target.host, target.path);
    } else {
      request = probe::ws_text_frame("{\"id\":\"" + std::to_string(++request_id) + "\"," +
                                     target.request + "}");
    }
    std::string response;
    const auto start = Clock::now();
    bool ok = conn.write_all(request, error);
    if (ok && target.kind == Kind::RestRtt) {
      int status = 0;
      ok = probe::read_http_response(conn, status, response, error);
      if (ok && status != 200) {
        ok = false;
        error = "http status " + std::to_string(status);
      }
    } else if (ok) {
      ok = probe::ws_read_message(conn, response, error);
      if (ok && response.find("\"status\":200") == std::string::npos) {
        ok = false;
        error = "ws-api response: " + response.substr(0, 200);
      }
    }
    const double rtt_us = since_us(start);
    if (!ok) {
      note_error(result, error);
      conn.close();
      connected = false;
      continue;
    }
    if (done >= options.warmup) {
      result.rtt_us.push_back(rtt_us);
    }
    ++done;
    sleep_ms(options.interval_ms);
  }
}

void measure_stream(SSL_CTX* ctx, const Target& target, const Options& options,
                    IpResult& result) {
  probe::TlsConnection conn;
  std::string error;
  if (!open_session(ctx, target, result.ip, options, conn, error)) {
    note_error(result, error);
    return;
  }
  const auto deadline = Clock::now() + std::chrono::seconds(options.stream_seconds);
  std::string message;
  while (Clock::now() < deadline) {
    if (!probe::ws_read_message(conn, message, error)) {
      note_error(result, error);
      return;
    }
    const double received_us =
        std::chrono::duration<double, std::micro>(
            std::chrono::system_clock::now().time_since_epoch())
            .count();
    const std::size_t at = message.find("\"E\":");
    if (at == std::string::npos) {
      continue;
    }
    const double event_ms = std::strtod(message.c_str() + at + 4, nullptr);
    if (event_ms > 0) {
      result.lag_us.push_back(received_us - event_ms * 1000.0);
    }
  }
}

std::string utc_now() {
  const std::time_t now = std::time(nullptr);
  std::tm parts{};
  gmtime_r(&now, &parts);
  char text[32];
  std::strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%SZ", &parts);
  return text;
}

std::string json_string(const std::string& text) {
  std::string out = "\"";
  for (unsigned char value : text) {
    if (value == '"') {
      out += "\\\"";
    } else if (value == '\\') {
      out += "\\\\";
    } else if (value < 0x20) {
      char escaped[8];
      std::snprintf(escaped, sizeof(escaped), "\\u%04x", value);
      out += escaped;
    } else {
      out.push_back(static_cast<char>(value));
    }
  }
  return out + "\"";
}

void write_summary(std::ostream& out, const std::vector<double>& values) {
  if (values.empty()) {
    out << "null";
    return;
  }
  const probe::Summary summary = probe::summarize(values);
  out << "{\"count\":" << summary.count << ",\"min\":" << summary.min
      << ",\"p50\":" << summary.p50 << ",\"p90\":" << summary.p90 << ",\"p99\":" << summary.p99
      << ",\"max\":" << summary.max << ",\"mean\":" << summary.mean << "}";
}

void write_report(std::ostream& out, const Options& options, const std::string& started_at,
                  const std::string& finished_at, const std::vector<TargetResult>& results) {
  out << std::fixed << std::setprecision(1);
  out << "{\"schema\":\"aws_ec2_select.probe.v1\",\"market\":" << json_string(options.market)
      << ",\"label\":" << json_string(options.label) << ",\"started_at\":"
      << json_string(started_at) << ",\"finished_at\":" << json_string(finished_at)
      << ",\"config\":{\"samples\":" << options.samples << ",\"warmup\":" << options.warmup
      << ",\"connect_samples\":" << options.connect_samples
      << ",\"interval_ms\":" << options.interval_ms << ",\"timeout_ms\":" << options.timeout_ms
      << ",\"stream_seconds\":" << options.stream_seconds << "},\"targets\":[";
  for (std::size_t t = 0; t < results.size(); ++t) {
    const TargetResult& result = results[t];
    out << (t == 0 ? "" : ",") << "{\"name\":" << json_string(result.target.name)
        << ",\"kind\":\"" << kind_name(result.target.kind)
        << "\",\"host\":" << json_string(result.target.host) << ",\"port\":" << result.target.port
        << ",\"path\":" << json_string(result.target.path)
        << ",\"clock_dependent\":" << (result.target.kind == Kind::StreamLag ? "true" : "false")
        << ",\"resolve_error\":" << json_string(result.resolve_error) << ",\"ips\":[";
    for (std::size_t i = 0; i < result.ips.size(); ++i) {
      const IpResult& ip = result.ips[i];
      out << (i == 0 ? "" : ",") << "{\"ip\":" << json_string(ip.ip)
          << ",\"errors\":" << ip.errors << ",\"first_error\":" << json_string(ip.first_error)
          << ",\"tcp_connect_us\":";
      write_summary(out, ip.tcp_us);
      out << ",\"tls_handshake_us\":";
      write_summary(out, ip.tls_us);
      out << ",\"rtt_us\":";
      write_summary(out, ip.rtt_us);
      out << ",\"stream_lag_us\":";
      write_summary(out, ip.lag_us);
      out << "}";
    }
    out << "]}";
  }
  out << "]}\n";
}

bool selected(const Options& options, const std::string& name) {
  if (options.only.empty()) {
    return true;
  }
  for (const std::string& item : options.only) {
    if (item == name) {
      return true;
    }
  }
  return false;
}

}  // namespace

int main(int argc, char** argv) {
  std::signal(SIGPIPE, SIG_IGN);
  Options options;
  if (!parse_args(argc, argv, options)) {
    usage();
    return 64;
  }
  std::vector<Target> targets = options.market == "futures" ? futures_targets() : spot_targets();
  for (const std::string& spec : options.sets) {
    std::string error;
    if (!apply_set(targets, spec, error)) {
      std::fprintf(stderr, "%s\n", error.c_str());
      return 64;
    }
  }

  std::string error;
  SSL_CTX* ctx = probe::make_tls_context(error);
  if (ctx == nullptr) {
    std::fprintf(stderr, "tls context: %s\n", error.c_str());
    return 70;
  }

  const std::string started_at = utc_now();
  std::vector<TargetResult> results;
  int measured = 0;
  for (const Target& target : targets) {
    if (!selected(options, target.name)) {
      continue;
    }
    TargetResult result{target, "", {}};
    std::string resolve_error;
    for (const std::string& ip : probe::resolve_ipv4(target.host, target.port, resolve_error)) {
      IpResult ip_result;
      ip_result.ip = ip;
      measure_connect(ctx, target, options, ip_result);
      if (target.kind == Kind::StreamLag) {
        measure_stream(ctx, target, options, ip_result);
      } else {
        measure_rtt(ctx, target, options, ip_result);
      }
      const std::vector<double>& main_metric =
          target.kind == Kind::StreamLag ? ip_result.lag_us : ip_result.rtt_us;
      if (!main_metric.empty()) {
        ++measured;
        std::fprintf(stderr, "%-7s %-15s %-26s p50=%.1fus n=%zu errors=%d\n",
                     options.market.c_str(), target.name.c_str(), ip.c_str(),
                     probe::summarize(main_metric).p50, main_metric.size(), ip_result.errors);
      } else {
        std::fprintf(stderr, "%-7s %-15s %-26s no samples: %s\n", options.market.c_str(),
                     target.name.c_str(), ip.c_str(), ip_result.first_error.c_str());
      }
      result.ips.push_back(ip_result);
    }
    result.resolve_error = resolve_error;
    results.push_back(result);
  }
  SSL_CTX_free(ctx);

  const std::string finished_at = utc_now();
  if (options.out.empty()) {
    write_report(std::cout, options, started_at, finished_at, results);
  } else {
    std::ofstream file(options.out);
    if (!file) {
      std::fprintf(stderr, "cannot write %s\n", options.out.c_str());
      return 73;
    }
    write_report(file, options, started_at, finished_at, results);
  }
  return measured > 0 ? 0 : 2;
}
