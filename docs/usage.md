# 使用说明

真实测速只在你自己的机器上运行；GitHub Actions 只做编译、单元测试和 dry-run，不持有任何 AWS 凭证。

## 前置条件

- Python 3.10+，`pip install boto3`。
- 本机已配置 AWS 凭证（`aws configure` 或 `--profile`），权限见 [iam.md](iam.md)。
- 一个实例配置文件（instance profile），挂 `AmazonSSMManagedInstanceCore`，供 SSM 下发命令。
- 目标可用区有默认子网，或用 `--subnet AZ=subnet-...` 指定；子网需能出公网（公网 IP 或 NAT）。
- 不需要开放任何入站端口，也不需要 SSH 密钥。

## 流程

1. 按 `--az` × `--per-az` 启动 `c7i-flex.large`（默认），打 `Project` / `GachaRun` / `ExpiresAt` 标签。
2. user data（`userdata/bootstrap.sh`）先执行 `shutdown -h +TTL` 自毁定时，再拉代码、编译探针、等待 chrony 同步。
3. 等 SSM 上线后下发探针：合约、现货各跑一次，结果以 `REPORT <market> <base64(gzip(json))>` 回传。
4. 打分、保留最优 `--keep` 台（取消自毁定时、去掉 `ExpiresAt`、打 `Keep=true`），其余立即 terminate。
5. 任何异常都会在 `finally` 中 terminate 全部未保留实例。

## 示例

```bash
cd gacha

# dry-run（默认）：只打印计划和探针命令，不调用 AWS
python ec2_gacha.py run --az ap-northeast-1a --az ap-northeast-1c --az ap-northeast-1d

# 真实运行：每个可用区 1 台，保留最优 1 台
python ec2_gacha.py --profile my-profile run --execute \
  --instance-profile ec2-ssm-probe \
  --az ap-northeast-1a --az ap-northeast-1c --az ap-northeast-1d \
  --git-ref main --max-p50-us-futures 2000 --max-p50-us-spot 2000

# 只测合约，全部销毁只看报告
python ec2_gacha.py run --execute --instance-profile ec2-ssm-probe --markets futures --keep 0

# 兜底清理：列出过期或缺 ExpiresAt 的实例；加 --execute 才会 terminate
python ec2_gacha.py cleanup
python ec2_gacha.py cleanup --run-id 20261125T080000Z-abc123 --execute
```

功能分支合并前，实例上需要用 `--git-ref feat/ec2-latency-gacha` 拉代码（也可传提交 SHA）。

## 结果

`results/<run_id>/` 下保存 `summary.json` 和每台实例每个市场的原始报告 `<instance>_<market>.json`，终端打印汇总表。

## 安全阀

| 机制 | 默认 |
| --- | --- |
| dry-run | 不加 `--execute` 不开机 |
| 台数上限 | `--max-total 4`，硬上限 20 |
| 实例自毁 | `--ttl-minutes 60`（15–720），关机即 terminate |
| 异常兜底 | `finally` terminate 全部未保留实例 |
| 离线回收 | `cleanup` 按 `ExpiresAt` 标签回收 |

## 探针单独使用

```bash
cmake -S probe -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
./build/latency_probe --market futures --samples 200 --out futures.json
./build/latency_probe --market spot --only ws_api,rest --set ws_api.host=ws-api.binance.com
```

| 市场 | rest | ws_api | stream |
| --- | --- | --- | --- |
| futures | `fapi.binance.com/fapi/v1/time` | `ws-fapi.binance.com/ws-fapi/v1` `ticker.book` | `fstream.binance.com/ws/btcusdt@bookTicker` |
| spot | `api.binance.com/api/v3/time` | `ws-api.binance.com/ws-api/v3` `time` | `stream.binance.com:9443/ws/btcusdt@trade` |

全部是公开接口，不需要 API key，不下单。
