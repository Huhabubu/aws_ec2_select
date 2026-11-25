#include "ws_frame.hpp"

#include <cstdio>
#include <string>

namespace {

int failures = 0;

void expect(bool condition, const char* label) {
  if (!condition) {
    std::printf("FAIL %s\n", label);
    ++failures;
  }
}

std::string unmask(const std::string& frame, const probe::FrameHeader& header) {
  const std::string mask = frame.substr(header.header_length - 4, 4);
  std::string payload = frame.substr(header.header_length);
  for (std::size_t index = 0; index < payload.size(); ++index) {
    payload[index] = static_cast<char>(payload[index] ^ mask[index % 4]);
  }
  return payload;
}

void round_trip(std::size_t size, std::size_t expected_header, const char* label) {
  const unsigned char mask[4] = {0x12, 0x34, 0x56, 0x78};
  const std::string payload(size, 'x');
  const std::string frame = probe::encode_client_frame(0x1, payload, mask);
  probe::FrameHeader header;
  expect(probe::parse_frame_header(frame, header), label);
  expect(header.fin, label);
  expect(header.opcode == 0x1, label);
  expect(header.masked, label);
  expect(header.payload_length == size, label);
  expect(header.header_length == expected_header, label);
  expect(frame.size() == expected_header + size, label);
  expect(unmask(frame, header) == payload, label);
}

}  // namespace

int main() {
  round_trip(0, 6, "empty payload");
  round_trip(125, 6, "7-bit length");
  round_trip(126, 8, "16-bit length lower bound");
  round_trip(65535, 8, "16-bit length upper bound");
  round_trip(65536, 14, "64-bit length");

  const std::string server_frame = std::string("\x81\x02", 2) + "ok";
  probe::FrameHeader header;
  expect(probe::parse_frame_header(server_frame, header), "server frame parse");
  expect(!header.masked && header.payload_length == 2 && header.header_length == 2,
         "server frame fields");

  expect(!probe::parse_frame_header(std::string("\x81", 1), header), "short header");
  expect(!probe::parse_frame_header(std::string("\x81\x7e\x00", 3), header), "short 16-bit");

  if (failures == 0) {
    std::printf("test_ws_frame: all passed\n");
  }
  return failures == 0 ? 0 : 1;
}
