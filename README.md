# aws_ec2_select

在东京区（`ap-northeast-1`）批量“抽卡”启动 EC2，逐台测量到币安合约和现货端点的延迟，保留最优实例，其余立即销毁。

- `probe/`：C++17 + OpenSSL 延迟探针，逐 IP 测 TCP 建连、TLS 握手、REST / WS-API 往返和行情流延迟，输出 JSON。
- `gacha/`：Python + boto3 抽卡控制器，通过 SSM 下发探针，打分、保留、销毁，带 `cleanup` 兜底回收。
- `userdata/bootstrap.sh`：实例启动脚本，先设自毁定时，再编译探针。
- `docs/`：[使用说明](docs/usage.md)、[打分口径](docs/scoring.md)、[最小 IAM 权限](docs/iam.md)。

默认 dry-run，必须显式加 `--execute` 才会创建或销毁资源。CI 只做编译、单元测试和 dry-run，不接触 AWS。
