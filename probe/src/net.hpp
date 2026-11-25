#pragma once

#include <openssl/ssl.h>

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace probe {

std::vector<std::string> resolve_ipv4(const std::string& host, std::uint16_t port,
                                      std::string& error);

SSL_CTX* make_tls_context(std::string& error);

void random_bytes(unsigned char* out, int size);

class TlsConnection {
 public:
  TlsConnection() = default;
  ~TlsConnection();
  TlsConnection(const TlsConnection&) = delete;
  TlsConnection& operator=(const TlsConnection&) = delete;

  bool open(SSL_CTX* ctx, const std::string& ip, std::uint16_t port, const std::string& host,
            int timeout_ms, double& tcp_us, double& tls_us, std::string& error);
  bool write_all(const std::string& data, std::string& error);
  bool read_until(const std::string& delimiter, std::string& out, std::string& error);
  bool read_n(std::size_t size, std::string& out, std::string& error);
  void close();

 private:
  bool fill(std::string& error);

  int fd_ = -1;
  SSL* ssl_ = nullptr;
  std::string buffer_;
};

std::string build_http_get(const std::string& host, const std::string& path);
bool read_http_response(TlsConnection& conn, int& status, std::string& body, std::string& error);

bool ws_handshake(TlsConnection& conn, const std::string& host, const std::string& path,
                  std::string& error);
std::string ws_text_frame(const std::string& payload);
bool ws_read_message(TlsConnection& conn, std::string& message, std::string& error);

}  // namespace probe
