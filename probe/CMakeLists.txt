cmake_minimum_required(VERSION 3.16)
project(aws_ec2_select_probe CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(OpenSSL REQUIRED)

add_executable(latency_probe src/main.cpp src/net.cpp)
target_compile_options(latency_probe PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(latency_probe PRIVATE OpenSSL::SSL OpenSSL::Crypto)

enable_testing()
add_executable(test_stats tests/test_stats.cpp)
target_include_directories(test_stats PRIVATE src)
target_compile_options(test_stats PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME test_stats COMMAND test_stats)

add_executable(test_ws_frame tests/test_ws_frame.cpp)
target_include_directories(test_ws_frame PRIVATE src)
target_compile_options(test_ws_frame PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME test_ws_frame COMMAND test_ws_frame)
