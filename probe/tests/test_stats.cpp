#include "stats.hpp"

#include <cmath>
#include <cstdio>
#include <vector>

namespace {

int failures = 0;

void expect_near(double actual, double expected, const char* label) {
  if (std::fabs(actual - expected) > 1e-9) {
    std::printf("FAIL %s: got %.6f expected %.6f\n", label, actual, expected);
    ++failures;
  }
}

}  // namespace

int main() {
  std::vector<double> hundred;
  for (int value = 100; value >= 1; --value) {
    hundred.push_back(value);
  }
  probe::Summary full = probe::summarize(hundred);
  expect_near(static_cast<double>(full.count), 100, "count");
  expect_near(full.min, 1, "min");
  expect_near(full.max, 100, "max");
  expect_near(full.p50, 50, "p50 of 1..100");
  expect_near(full.p90, 90, "p90 of 1..100");
  expect_near(full.p99, 99, "p99 of 1..100");
  expect_near(full.mean, 50.5, "mean of 1..100");

  probe::Summary ten = probe::summarize({1, 2, 3, 4, 5, 6, 7, 8, 9, 10});
  expect_near(ten.p50, 5, "p50 of 1..10");
  expect_near(ten.p90, 9, "p90 of 1..10");
  expect_near(ten.p99, 10, "p99 of 1..10");

  probe::Summary single = probe::summarize({42});
  expect_near(single.p50, 42, "p50 single");
  expect_near(single.p99, 42, "p99 single");

  probe::Summary empty = probe::summarize({});
  expect_near(static_cast<double>(empty.count), 0, "empty count");

  if (failures == 0) {
    std::printf("test_stats: all passed\n");
  }
  return failures == 0 ? 0 : 1;
}
