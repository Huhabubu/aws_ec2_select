#pragma once

#include <algorithm>
#include <cstddef>
#include <vector>

namespace probe {

struct Summary {
  std::size_t count = 0;
  double min = 0.0;
  double p50 = 0.0;
  double p90 = 0.0;
  double p99 = 0.0;
  double max = 0.0;
  double mean = 0.0;
};

inline double nearest_rank(const std::vector<double>& sorted, unsigned pct) {
  if (sorted.empty()) {
    return 0.0;
  }
  std::size_t rank = (static_cast<std::size_t>(pct) * sorted.size() + 99) / 100;
  rank = std::max<std::size_t>(1, std::min(rank, sorted.size()));
  return sorted[rank - 1];
}

inline Summary summarize(std::vector<double> values) {
  Summary summary;
  if (values.empty()) {
    return summary;
  }
  std::sort(values.begin(), values.end());
  double total = 0.0;
  for (double value : values) {
    total += value;
  }
  summary.count = values.size();
  summary.min = values.front();
  summary.max = values.back();
  summary.mean = total / static_cast<double>(values.size());
  summary.p50 = nearest_rank(values, 50);
  summary.p90 = nearest_rank(values, 90);
  summary.p99 = nearest_rank(values, 99);
  return summary;
}

}  // namespace probe
