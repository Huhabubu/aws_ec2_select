#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace probe {

struct FrameHeader {
  bool fin = false;
  unsigned char opcode = 0;
  bool masked = false;
  std::uint64_t payload_length = 0;
  std::size_t header_length = 0;
};

inline std::string encode_client_frame(unsigned char opcode, const std::string& payload,
                                       const unsigned char mask[4]) {
  std::string frame;
  frame.push_back(static_cast<char>(0x80 | (opcode & 0x0F)));
  const std::uint64_t length = payload.size();
  if (length < 126) {
    frame.push_back(static_cast<char>(0x80 | length));
  } else if (length <= 0xFFFF) {
    frame.push_back(static_cast<char>(0x80 | 126));
    frame.push_back(static_cast<char>((length >> 8) & 0xFF));
    frame.push_back(static_cast<char>(length & 0xFF));
  } else {
    frame.push_back(static_cast<char>(0x80 | 127));
    for (int shift = 56; shift >= 0; shift -= 8) {
      frame.push_back(static_cast<char>((length >> shift) & 0xFF));
    }
  }
  frame.append(reinterpret_cast<const char*>(mask), 4);
  for (std::size_t index = 0; index < payload.size(); ++index) {
    frame.push_back(static_cast<char>(payload[index] ^ static_cast<char>(mask[index % 4])));
  }
  return frame;
}

inline bool parse_frame_header(const std::string& data, FrameHeader& header) {
  if (data.size() < 2) {
    return false;
  }
  const auto byte_at = [&data](std::size_t index) {
    return static_cast<std::uint64_t>(static_cast<unsigned char>(data[index]));
  };
  header.fin = (byte_at(0) & 0x80) != 0;
  header.opcode = static_cast<unsigned char>(byte_at(0) & 0x0F);
  header.masked = (byte_at(1) & 0x80) != 0;
  std::uint64_t length = byte_at(1) & 0x7F;
  std::size_t position = 2;
  if (length == 126) {
    if (data.size() < 4) {
      return false;
    }
    length = (byte_at(2) << 8) | byte_at(3);
    position = 4;
  } else if (length == 127) {
    if (data.size() < 10) {
      return false;
    }
    length = 0;
    for (std::size_t index = 2; index < 10; ++index) {
      length = (length << 8) | byte_at(index);
    }
    position = 10;
  }
  if (header.masked) {
    position += 4;
  }
  if (data.size() < position) {
    return false;
  }
  header.payload_length = length;
  header.header_length = position;
  return true;
}

}  // namespace probe
