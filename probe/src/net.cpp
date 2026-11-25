#include "net.hpp"

#include "ws_frame.hpp"

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/rand.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <set>

namespace probe {
namespace {

constexpr std::size_t kMaxMessageBytes = 16 * 1024 * 1024;

double elapsed_us(std::chrono::steady_clock::time_point start) {
  return std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - start)
      .count();
}

std::string openssl_error() {
  const unsigned long code = ERR_get_error();
  if (code == 0) {
    return "unknown openssl error";
  }
  char text[256];
  ERR_error_string_n(code, text, sizeof(text));
  return text;
}

std::string lowercase(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(),
                 [](unsigned char value) { return static_cast<char>(std::tolower(value)); });
  return text;
}

std::string first_line(const std::string& text) { return text.substr(0, text.find("\r\n")); }

}  // namespace

std::vector<std::string> resolve_ipv4(const std::string& host, std::uint16_t port,
                                      std::string& error) {
  addrinfo hints{};
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* result = nullptr;
  const std::string service = std::to_string(port);
  std::vector<std::string> ips;
  const int rc = getaddrinfo(host.c_str(), service.c_str(), &hints, &result);
  if (rc != 0) {
    error = gai_strerror(rc);
    return ips;
  }
  std::set<std::string> seen;
  for (addrinfo* item = result; item != nullptr; item = item->ai_next) {
    char text[INET_ADDRSTRLEN];
    const auto* address = reinterpret_cast<const sockaddr_in*>(item->ai_addr);
    if (inet_ntop(AF_INET, &address->sin_addr, text, sizeof(text)) != nullptr &&
        seen.insert(text).second) {
      ips.emplace_back(text);
    }
  }
  freeaddrinfo(result);
  return ips;
}

SSL_CTX* make_tls_context(std::string& error) {
  SSL_CTX* ctx = SSL_CTX_new(TLS_client_method());
  if (ctx == nullptr) {
    error = openssl_error();
    return nullptr;
  }
  SSL_CTX_set_min_proto_version(ctx, TLS1_2_VERSION);
  SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, nullptr);
  SSL_CTX_set_session_cache_mode(ctx, SSL_SESS_CACHE_OFF);
  if (SSL_CTX_set_default_verify_paths(ctx) != 1) {
    error = openssl_error();
    SSL_CTX_free(ctx);
    return nullptr;
  }
  return ctx;
}

void random_bytes(unsigned char* out, int size) {
  if (RAND_bytes(out, size) != 1) {
    for (int index = 0; index < size; ++index) {
      out[index] = static_cast<unsigned char>(std::rand() & 0xFF);
    }
  }
}

TlsConnection::~TlsConnection() { close(); }

bool TlsConnection::open(SSL_CTX* ctx, const std::string& ip, std::uint16_t port,
                         const std::string& host, int timeout_ms, double& tcp_us, double& tls_us,
                         std::string& error) {
  close();
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(port);
  if (inet_pton(AF_INET, ip.c_str(), &address.sin_addr) != 1) {
    error = "invalid ip " + ip;
    return false;
  }
  fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd_ < 0) {
    error = std::string("socket: ") + std::strerror(errno);
    return false;
  }
  timeval timeout{};
  timeout.tv_sec = timeout_ms / 1000;
  timeout.tv_usec = (timeout_ms % 1000) * 1000;
  ::setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  ::setsockopt(fd_, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
  const int enable = 1;
  ::setsockopt(fd_, IPPROTO_TCP, TCP_NODELAY, &enable, sizeof(enable));

  auto start = std::chrono::steady_clock::now();
  if (::connect(fd_, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
    error = std::string("connect: ") + std::strerror(errno);
    close();
    return false;
  }
  tcp_us = elapsed_us(start);

  ssl_ = SSL_new(ctx);
  if (ssl_ == nullptr) {
    error = "SSL_new: " + openssl_error();
    close();
    return false;
  }
  SSL_set_fd(ssl_, fd_);
  SSL_set_tlsext_host_name(ssl_, host.c_str());
  SSL_set1_host(ssl_, host.c_str());
  start = std::chrono::steady_clock::now();
  if (SSL_connect(ssl_) != 1) {
    error = "tls: " + openssl_error();
    close();
    return false;
  }
  tls_us = elapsed_us(start);
  return true;
}

bool TlsConnection::write_all(const std::string& data, std::string& error) {
  std::size_t sent = 0;
  while (sent < data.size()) {
    const int chunk = static_cast<int>(std::min<std::size_t>(data.size() - sent, 1 << 20));
    const int written = SSL_write(ssl_, data.data() + sent, chunk);
    if (written <= 0) {
      error = "write: " + openssl_error();
      return false;
    }
    sent += static_cast<std::size_t>(written);
  }
  return true;
}

bool TlsConnection::fill(std::string& error) {
  char chunk[16384];
  const int received = SSL_read(ssl_, chunk, sizeof(chunk));
  if (received <= 0) {
    const int code = SSL_get_error(ssl_, received);
    if (code == SSL_ERROR_ZERO_RETURN) {
      error = "connection closed by peer";
    } else if (code == SSL_ERROR_SYSCALL) {
      error = std::string("read: ") + (errno != 0 ? std::strerror(errno) : "eof");
    } else {
      error = "read: " + openssl_error();
    }
    return false;
  }
  buffer_.append(chunk, static_cast<std::size_t>(received));
  return true;
}

bool TlsConnection::read_until(const std::string& delimiter, std::string& out,
                               std::string& error) {
  std::size_t position = std::string::npos;
  while ((position = buffer_.find(delimiter)) == std::string::npos) {
    if (buffer_.size() > kMaxMessageBytes) {
      error = "header too large";
      return false;
    }
    if (!fill(error)) {
      return false;
    }
  }
  const std::size_t end = position + delimiter.size();
  out = buffer_.substr(0, end);
  buffer_.erase(0, end);
  return true;
}

bool TlsConnection::read_n(std::size_t size, std::string& out, std::string& error) {
  if (size > kMaxMessageBytes) {
    error = "message too large";
    return false;
  }
  while (buffer_.size() < size) {
    if (!fill(error)) {
      return false;
    }
  }
  out = buffer_.substr(0, size);
  buffer_.erase(0, size);
  return true;
}

void TlsConnection::close() {
  if (ssl_ != nullptr) {
    SSL_free(ssl_);
    ssl_ = nullptr;
  }
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
  buffer_.clear();
}

std::string build_http_get(const std::string& host, const std::string& path) {
  return "GET " + path + " HTTP/1.1\r\nHost: " + host +
         "\r\nUser-Agent: aws-ec2-select-probe/0.1\r\nAccept: */*\r\nConnection: keep-alive\r\n\r\n";
}

bool read_http_response(TlsConnection& conn, int& status, std::string& body, std::string& error) {
  std::string head;
  if (!conn.read_until("\r\n\r\n", head, error)) {
    return false;
  }
  const std::size_t space = head.find(' ');
  if (head.compare(0, 5, "HTTP/") != 0 || space == std::string::npos) {
    error = "bad status line: " + first_line(head);
    return false;
  }
  status = std::atoi(head.c_str() + space + 1);
  const std::string lowered = lowercase(head);
  body.clear();
  const std::string length_key = "\r\ncontent-length:";
  const std::size_t length_at = lowered.find(length_key);
  if (length_at != std::string::npos) {
    const auto length = std::strtoull(head.c_str() + length_at + length_key.size(), nullptr, 10);
    return conn.read_n(static_cast<std::size_t>(length), body, error);
  }
  if (lowered.find("\r\ntransfer-encoding: chunked") != std::string::npos) {
    for (;;) {
      std::string line;
      if (!conn.read_until("\r\n", line, error)) {
        return false;
      }
      const auto size = static_cast<std::size_t>(std::strtoull(line.c_str(), nullptr, 16));
      std::string chunk;
      if (!conn.read_n(size + 2, chunk, error)) {
        return false;
      }
      if (size == 0) {
        return true;
      }
      body.append(chunk, 0, size);
    }
  }
  error = "response without content-length or chunked encoding";
  return false;
}

bool ws_handshake(TlsConnection& conn, const std::string& host, const std::string& path,
                  std::string& error) {
  unsigned char key[16];
  random_bytes(key, sizeof(key));
  unsigned char encoded[32];
  const int encoded_size = EVP_EncodeBlock(encoded, key, sizeof(key));
  const std::string request =
      "GET " + path + " HTTP/1.1\r\nHost: " + host +
      "\r\nUser-Agent: aws-ec2-select-probe/0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
      "Sec-WebSocket-Key: " +
      std::string(reinterpret_cast<const char*>(encoded), static_cast<std::size_t>(encoded_size)) +
      "\r\nSec-WebSocket-Version: 13\r\n\r\n";
  if (!conn.write_all(request, error)) {
    return false;
  }
  std::string head;
  if (!conn.read_until("\r\n\r\n", head, error)) {
    return false;
  }
  if (first_line(head).find(" 101") == std::string::npos) {
    error = "websocket upgrade failed: " + first_line(head);
    return false;
  }
  return true;
}

std::string ws_text_frame(const std::string& payload) {
  unsigned char mask[4];
  random_bytes(mask, sizeof(mask));
  return encode_client_frame(0x1, payload, mask);
}

bool ws_read_message(TlsConnection& conn, std::string& message, std::string& error) {
  message.clear();
  for (;;) {
    std::string header;
    if (!conn.read_n(2, header, error)) {
      return false;
    }
    const auto second = static_cast<unsigned char>(header[1]);
    const unsigned char short_length = second & 0x7F;
    std::size_t extra = short_length == 126 ? 2 : (short_length == 127 ? 8 : 0);
    if ((second & 0x80) != 0) {
      extra += 4;
    }
    if (extra > 0) {
      std::string rest;
      if (!conn.read_n(extra, rest, error)) {
        return false;
      }
      header += rest;
    }
    FrameHeader frame;
    if (!parse_frame_header(header, frame) || frame.payload_length > kMaxMessageBytes) {
      error = "invalid websocket frame header";
      return false;
    }
    std::string payload;
    if (!conn.read_n(static_cast<std::size_t>(frame.payload_length), payload, error)) {
      return false;
    }
    if (frame.masked) {
      const std::string mask = header.substr(frame.header_length - 4, 4);
      for (std::size_t index = 0; index < payload.size(); ++index) {
        payload[index] = static_cast<char>(payload[index] ^ mask[index % 4]);
      }
    }
    if (frame.opcode == 0x9) {
      unsigned char mask[4];
      random_bytes(mask, sizeof(mask));
      if (!conn.write_all(encode_client_frame(0xA, payload, mask), error)) {
        return false;
      }
      continue;
    }
    if (frame.opcode == 0xA) {
      continue;
    }
    if (frame.opcode == 0x8) {
      error = "websocket closed by server";
      return false;
    }
    message += payload;
    if (frame.fin) {
      return true;
    }
  }
}

}  // namespace probe
