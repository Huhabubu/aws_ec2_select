# 打分口径

## 采样

- 每个端点先解析全部 IPv4，逐 IP 测量。
- `tcp_connect_us` / `tls_handshake_us`：每 IP 新建 `--connect-samples` 次连接。
- `rtt_us`：在一条长连接上发送请求到收到完整响应，先丢弃 `--warmup` 个样本，再记录 `--samples` 个。连接出错会重连，错误数超过 $3 + \lfloor N/10 \rfloor$ 时该 IP 停止。
- `stream_lag_us`：本机接收时刻减去消息里的事件时间 `E`，依赖本机与币安的时钟差，只作参考，不参与打分（报告中 `clock_dependent=true`）。

## 分位数

采用最近秩法（nearest-rank）。样本升序排列为 $x_{(1)} \le \dots \le x_{(n)}$，第 $p$ 百分位为

$$
Q_p = x_{(k)},\qquad k = \max\left(1, \left\lceil \frac{p}{100}\, n \right\rceil\right)
$$

## 单市场得分

- 主指标为 `ws_api` 的 RTT（下单走的就是这类长连接请求-响应路径）。
- 只统计样本数 $\ge$ `--min-samples` 的 IP；取 $Q_{50}$ 最小的 IP 作为该市场的最优 IP，同时记录它的 $Q_{99}$ 和全部 IP 中最差的 $Q_{50}$。
- 设置了 `--max-p50-us-<market>` 时，最优 IP 的 $Q_{50}$ 超过阈值即判为不达标。

## 实例排名

实例必须在所有选定市场都达标。达标实例按

$$
\left(\sum_{m} Q_{50}^{(m)},\ \sum_{m} Q_{99}^{(m)}\right)
$$

的字典序升序排列，取前 `--keep` 台。

## 已知局限

- c7i-flex 是基准性能加突发的机型，CPU 调度抖动可能抬高 $Q_{99}$，排名以 $Q_{50}$ 为主。
- 单次测量只反映当时的路由与负载，建议同一可用区多抽几台、分时段复测。
