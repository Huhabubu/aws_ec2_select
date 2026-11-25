#!/usr/bin/env python3
"""Launch EC2 instances, probe Binance latency through SSM, keep the best, terminate the rest.

Dry-run by default: nothing is launched or terminated unless --execute is given.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shlex
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from scoring import MARKETS, InstanceScore, evaluate, parse_report_lines, select_keep

PROJECT = "aws_ec2_select"
DEFAULT_REGION = "ap-northeast-1"
DEFAULT_AZS = ("ap-northeast-1a", "ap-northeast-1c", "ap-northeast-1d")
DEFAULT_INSTANCE_TYPE = "c7i-flex.large"
DEFAULT_AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
DEFAULT_REPO_URL = "https://github.com/Huhabubu/aws_ec2_select.git"
HARD_MAX_TOTAL = 20
SSM_STDOUT_LIMIT = 24000
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USER_DATA = REPO_ROOT / "userdata" / "bootstrap.sh"
PROBE_BIN = "/opt/aws_ec2_select/build/latency_probe"
TERMINAL_STATUSES = {"Success", "Failed", "Cancelled", "TimedOut"}
PLACEHOLDERS = ("__REPO_URL__", "__GIT_REF__", "__TTL_MINUTES__")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime(ISO_FORMAT)


def is_expired(value: str | None, now: dt.datetime) -> bool:
    if not value:
        return True
    try:
        expires = dt.datetime.strptime(value, ISO_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return True
    return expires <= now


@dataclass
class Plan:
    run_id: str
    region: str
    instance_type: str
    azs: list[str]
    per_az: int
    keep: int
    ttl_minutes: int
    markets: list[str]
    thresholds: dict[str, float | None]
    min_samples: int

    @property
    def total(self) -> int:
        return self.per_az * len(self.azs)


def new_run_id(now: dt.datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def build_plan(args: argparse.Namespace, now: dt.datetime | None = None) -> Plan:
    azs = list(dict.fromkeys(args.az or DEFAULT_AZS))
    markets = [market for market in dict.fromkeys(args.markets.split(",")) if market]
    if not markets or any(market not in MARKETS for market in markets):
        raise ValueError(f"--markets must be a subset of {','.join(MARKETS)}")
    if args.per_az < 1:
        raise ValueError("--per-az must be >= 1")
    if not 1 <= args.max_total <= HARD_MAX_TOTAL:
        raise ValueError(f"--max-total must be within 1..{HARD_MAX_TOTAL}")
    total = args.per_az * len(azs)
    if total > args.max_total:
        raise ValueError(f"{total} instances requested exceeds --max-total {args.max_total}")
    if not 0 <= args.keep <= total:
        raise ValueError("--keep must be within 0..number of instances")
    if not 15 <= args.ttl_minutes <= 720:
        raise ValueError("--ttl-minutes must be within 15..720")
    if not 1 <= args.min_samples <= args.samples:
        raise ValueError("--min-samples must be within 1..--samples")
    thresholds = {"futures": args.max_p50_us_futures, "spot": args.max_p50_us_spot}
    return Plan(new_run_id(now or utcnow()), args.region, args.instance_type, azs, args.per_az,
                args.keep, args.ttl_minutes, markets,
                {market: thresholds[market] for market in markets}, args.min_samples)


def render_user_data(template: str, repo_url: str, git_ref: str, ttl_minutes: int) -> str:
    missing = [item for item in PLACEHOLDERS if item not in template]
    if missing:
        raise ValueError(f"user data template lacks placeholders: {', '.join(missing)}")
    for value in (repo_url, git_ref):
        if not value or any(char in value for char in "'\"`$\\ \n;&|<>"):
            raise ValueError(f"unsafe or empty value: {value!r}")
    return (template.replace("__REPO_URL__", repo_url)
            .replace("__GIT_REF__", git_ref)
            .replace("__TTL_MINUTES__", str(ttl_minutes)))


def probe_commands(markets: list[str], samples: int, stream_seconds: int,
                   extra_args: list[str]) -> list[str]:
    extra = " ".join(shlex.quote(arg) for arg in extra_args)
    commands = [
        "set -u",
        "cloud-init status --wait >/dev/null 2>&1 || true",
        f"BIN={PROBE_BIN}",
        'if [ ! -x "$BIN" ]; then echo "probe binary missing; see '
        '/var/log/aws_ec2_select_bootstrap.log" >&2; exit 3; fi',
        "ID=$(cloud-init query v1.instance_id 2>/dev/null || hostname)",
        "chronyc tracking 2>/dev/null | sed 's/^/CHRONY /' || true",
    ]
    for market in markets:
        out = f"/tmp/probe_{market}.json"
        log = f"/tmp/probe_{market}.log"
        commands.append(f'"$BIN" --market {market} --samples {samples} '
                        f'--stream-seconds {stream_seconds} --label "$ID" --out {out} {extra} '
                        f'2>{log}')
        commands.append(f'if [ -s {out} ]; then echo "REPORT {market} $(gzip -c {out} | base64 -w0)"; '
                        f'else echo "NOREPORT {market}"; tail -n 5 {log} >&2; fi')
    return commands


class Gacha:
    def __init__(self, ec2, ssm, plan: Plan, options: argparse.Namespace, log=print,
                 sleep=time.sleep, clock=time.monotonic, now=utcnow):
        self.ec2 = ec2
        self.ssm = ssm
        self.plan = plan
        self.options = options
        self.log = log
        self.sleep = sleep
        self.clock = clock
        self.now = now
        self.launched: list[str] = []
        self.kept: list[str] = []
        self.placement: dict[str, dict] = {}
        self.launch_errors: dict[str, str] = {}

    def run(self) -> dict:
        try:
            return self._run()
        finally:
            self._terminate_unkept()

    def _terminate_unkept(self) -> None:
        doomed = [iid for iid in self.launched if iid not in self.kept]
        if not doomed:
            return
        self.log(f"terminating {len(doomed)} instance(s): {' '.join(doomed)}")
        try:
            self.ec2.terminate_instances(InstanceIds=doomed)
        except Exception:
            self.log(f"TERMINATE FAILED. Run: ec2_gacha.py cleanup --run-id {self.plan.run_id} --execute")
            raise

    def _run(self) -> dict:
        ami = self.options.ami or self.ssm.get_parameter(
            Name=self.options.ami_parameter)["Parameter"]["Value"]
        template = Path(self.options.user_data).read_text(encoding="utf-8")
        user_data = render_user_data(template, self.options.repo_url, self.options.git_ref,
                                     self.plan.ttl_minutes)
        zones = self.ec2.describe_availability_zones(ZoneNames=self.plan.azs)
        az_ids = {zone["ZoneName"]: zone["ZoneId"] for zone in zones["AvailabilityZones"]}
        self.log(f"run {self.plan.run_id}: ami={ami} type={self.plan.instance_type}")
        for az in self.plan.azs:
            try:
                self._launch_in(az, ami, user_data)
            except Exception as exc:
                self.launch_errors[az] = f"{exc.__class__.__name__}: {exc}"
                self.log(f"launch failed in {az}: {self.launch_errors[az]}")
        if not self.launched:
            raise RuntimeError("no instance launched in any availability zone")
        self.ec2.get_waiter("instance_running").wait(InstanceIds=self.launched)
        self._describe_placement(az_ids)
        online = self._wait_ssm_online(self.launched)
        commands = probe_commands(self.plan.markets, self.options.samples,
                                  self.options.stream_seconds, self.options.probe_arg)
        outputs = self._run_command(online, commands, self.options.probe_timeout)

        scores: dict[str, InstanceScore] = {}
        details: dict[str, dict] = {}
        run_dir = Path(self.options.out_dir) / self.plan.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        for iid in self.launched:
            result = outputs.get(iid)
            if result is None:
                scores[iid] = InstanceScore(iid, reason="SSM offline; probe not run")
                details[iid] = {"ssm_status": "offline"}
                continue
            stdout = result["stdout"]
            if len(stdout) >= SSM_STDOUT_LIMIT:
                self.log(f"{iid}: SSM stdout may be truncated ({len(stdout)} chars)")
            reports, errors = parse_report_lines(stdout)
            for market, report in reports.items():
                (run_dir / f"{iid}_{market}.json").write_text(json.dumps(report, indent=1),
                                                               encoding="utf-8")
            score = evaluate(iid, reports, self.plan.markets, self.plan.thresholds,
                             self.plan.min_samples)
            if errors:
                score.reason = "; ".join(filter(None, [score.reason, *errors]))
            scores[iid] = score
            details[iid] = {
                "ssm_status": result["status"],
                "chrony": [line[7:] for line in stdout.splitlines() if line.startswith("CHRONY ")],
                "stderr_tail": result["stderr"][-2000:],
            }

        for winner in select_keep(list(scores.values()), self.plan.keep):
            if self._keep_instance(winner.instance_id):
                self.kept.append(winner.instance_id)

        summary = {
            "plan": {**asdict(self.plan), "total": self.plan.total},
            "ami": ami,
            "finished_at": format_time(self.now()),
            "launch_errors": self.launch_errors,
            "kept": self.kept,
            "instances": [
                {
                    "instance_id": iid,
                    **self.placement.get(iid, {}),
                    **details[iid],
                    "passed": scores[iid].passed,
                    "reason": scores[iid].reason,
                    "kept": iid in self.kept,
                    "markets": {market: asdict(value)
                                for market, value in scores[iid].markets.items()},
                }
                for iid in self.launched
            ],
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        self.log(format_table(summary))
        self.log(f"results: {run_dir}")
        return summary

    def _subnet_for(self, az: str) -> str:
        if az in self.options.subnet:
            return self.options.subnet[az]
        response = self.ec2.describe_subnets(Filters=[
            {"Name": "availability-zone", "Values": [az]},
            {"Name": "default-for-az", "Values": ["true"]},
        ])
        subnets = response.get("Subnets", [])
        if not subnets:
            raise RuntimeError(f"no default subnet in {az}; pass --subnet {az}=subnet-...")
        return subnets[0]["SubnetId"]

    def _launch_in(self, az: str, ami: str, user_data: str) -> None:
        expires = format_time(self.now() + dt.timedelta(minutes=self.plan.ttl_minutes))
        tags = [
            {"Key": "Project", "Value": PROJECT},
            {"Key": "GachaRun", "Value": self.plan.run_id},
            {"Key": "ExpiresAt", "Value": expires},
            {"Key": "Name", "Value": f"{PROJECT}-{self.plan.run_id}"},
        ]
        params = {
            "ImageId": ami,
            "InstanceType": self.plan.instance_type,
            "MinCount": 1,
            "MaxCount": self.plan.per_az,
            "SubnetId": self._subnet_for(az),
            "IamInstanceProfile": {"Name": self.options.instance_profile},
            "UserData": user_data,
            "InstanceInitiatedShutdownBehavior": "terminate",
            "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": tags},
                {"ResourceType": "volume", "Tags": tags},
            ],
        }
        if self.options.security_group:
            params["SecurityGroupIds"] = self.options.security_group
        response = self.ec2.run_instances(**params)
        ids = [item["InstanceId"] for item in response["Instances"]]
        self.launched.extend(ids)
        if len(ids) < self.plan.per_az:
            self.log(f"{az}: only {len(ids)}/{self.plan.per_az} instance(s) launched")
        self.log(f"{az}: launched {' '.join(ids)}")

    def _describe_placement(self, az_ids: dict[str, str]) -> None:
        response = self.ec2.describe_instances(InstanceIds=self.launched)
        for reservation in response.get("Reservations", []):
            for item in reservation.get("Instances", []):
                az = item.get("Placement", {}).get("AvailabilityZone", "")
                self.placement[item["InstanceId"]] = {
                    "az": az,
                    "az_id": az_ids.get(az, ""),
                    "public_ip": item.get("PublicIpAddress", ""),
                    "private_ip": item.get("PrivateIpAddress", ""),
                }

    def _wait_ssm_online(self, ids: list[str]) -> list[str]:
        deadline = self.clock() + self.options.ssm_timeout
        online: set[str] = set()
        while True:
            response = self.ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": list(ids)}])
            online = {item["InstanceId"] for item in response.get("InstanceInformationList", [])
                      if item.get("PingStatus") == "Online"}
            if online >= set(ids) or self.clock() >= deadline:
                break
            self.sleep(10)
        missing = [iid for iid in ids if iid not in online]
        if missing:
            self.log(f"SSM not online after {self.options.ssm_timeout}s: {' '.join(missing)}")
        if not online:
            raise RuntimeError("no instance registered with SSM; check the instance profile "
                               "and outbound network access")
        return [iid for iid in ids if iid in online]

    def _run_command(self, ids: list[str], commands: list[str], timeout: int) -> dict[str, dict]:
        response = self.ssm.send_command(
            InstanceIds=ids,
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
            TimeoutSeconds=300,
            Comment=f"{PROJECT} {self.plan.run_id}",
        )
        command_id = response["Command"]["CommandId"]
        deadline = self.clock() + timeout + 300
        pending = set(ids)
        results: dict[str, dict] = {}
        while pending:
            for iid in sorted(pending):
                try:
                    invocation = self.ssm.get_command_invocation(CommandId=command_id,
                                                                 InstanceId=iid)
                except Exception as exc:
                    if "InvocationDoesNotExist" in f"{exc.__class__.__name__} {exc}":
                        continue
                    raise
                if invocation.get("Status") in TERMINAL_STATUSES:
                    results[iid] = {
                        "status": invocation["Status"],
                        "stdout": invocation.get("StandardOutputContent", ""),
                        "stderr": invocation.get("StandardErrorContent", ""),
                    }
                    pending.discard(iid)
            if pending:
                if self.clock() >= deadline:
                    raise TimeoutError(f"SSM command {command_id} unfinished on {sorted(pending)}")
                self.sleep(5)
        return results

    def _keep_instance(self, iid: str) -> bool:
        try:
            result = self._run_command([iid], ["shutdown -c"], 60).get(iid, {})
        except Exception as exc:
            self.log(f"{iid}: cannot cancel self-destruct ({exc}); not keeping it")
            return False
        if result.get("status") != "Success":
            self.log(f"{iid}: cancel self-destruct returned {result.get('status')}; not keeping it")
            return False
        self.ec2.create_tags(Resources=[iid], Tags=[
            {"Key": "Keep", "Value": "true"},
            {"Key": "Name", "Value": f"{PROJECT}-keep-{self.plan.run_id}"},
        ])
        self.ec2.delete_tags(Resources=[iid], Tags=[{"Key": "ExpiresAt"}])
        self.log(f"{iid}: kept")
        return True


def format_table(summary: dict) -> str:
    markets = summary["plan"]["markets"]
    header = f"{'instance':<21}{'az':<17}{'az_id':<12}"
    header += "".join(f"{market + '_p50':>13}{market + '_p99':>13}" for market in markets)
    header += f"{'pass':>6}{'keep':>6}  reason"
    rows = [header]
    for item in summary["instances"]:
        row = f"{item['instance_id']:<21}{item.get('az', ''):<17}{item.get('az_id', ''):<12}"
        for market in markets:
            score = item["markets"].get(market, {})
            for key in ("p50_us", "p99_us"):
                value = score.get(key)
                row += f"{value:>13.1f}" if value is not None else f"{'-':>13}"
        row += f"{'yes' if item['passed'] else 'no':>6}{'yes' if item['kept'] else 'no':>6}"
        rows.append(row + f"  {item['reason']}")
    return "\n".join(rows)


def find_cleanup_targets(ec2, now: dt.datetime, run_id: str | None = None,
                         include_kept: bool = False) -> list[dict]:
    filters = [
        {"Name": "tag:Project", "Values": [PROJECT]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ]
    if run_id:
        filters.append({"Name": "tag:GachaRun", "Values": [run_id]})
    targets = []
    token = None
    while True:
        kwargs = {"Filters": filters}
        if token:
            kwargs["NextToken"] = token
        page = ec2.describe_instances(**kwargs)
        for reservation in page.get("Reservations", []):
            for item in reservation.get("Instances", []):
                tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}
                kept = tags.get("Keep") == "true"
                if kept and not include_kept:
                    continue
                if kept:
                    reason = "kept (included)"
                elif run_id:
                    reason = f"run {run_id}"
                elif is_expired(tags.get("ExpiresAt"), now):
                    reason = f"expired ({tags.get('ExpiresAt') or 'no ExpiresAt'})"
                else:
                    continue
                targets.append({"instance_id": item["InstanceId"],
                                "run_id": tags.get("GachaRun", ""), "reason": reason})
        token = page.get("NextToken")
        if not token:
            return targets


def parse_subnets(values: list[str]) -> dict[str, str]:
    mapping = {}
    for value in values:
        az, sep, subnet = value.partition("=")
        if not sep or not az or not subnet.startswith("subnet-"):
            raise ValueError(f"--subnet expects AZ=subnet-..., got {value!r}")
        mapping[az] = subnet
    return mapping


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", help="AWS CLI profile name")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="launch, probe, keep the best, terminate the rest")
    run.add_argument("--execute", action="store_true", help="actually launch (default: dry-run)")
    run.add_argument("--az", action="append", help=f"repeatable; default {','.join(DEFAULT_AZS)}")
    run.add_argument("--per-az", type=int, default=1)
    run.add_argument("--max-total", type=int, default=4)
    run.add_argument("--keep", type=int, default=1)
    run.add_argument("--ttl-minutes", type=int, default=60)
    run.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE)
    run.add_argument("--instance-profile", help="IAM instance profile with SSM core permissions")
    run.add_argument("--ami", help="AMI id; default resolves --ami-parameter")
    run.add_argument("--ami-parameter", default=DEFAULT_AMI_PARAMETER)
    run.add_argument("--subnet", action="append", default=[], help="repeatable AZ=subnet-...")
    run.add_argument("--security-group", action="append", default=[], help="repeatable sg-...")
    run.add_argument("--markets", default=",".join(MARKETS))
    run.add_argument("--max-p50-us-futures", type=float)
    run.add_argument("--max-p50-us-spot", type=float)
    run.add_argument("--samples", type=int, default=200)
    run.add_argument("--min-samples", type=int, default=50)
    run.add_argument("--stream-seconds", type=int, default=10)
    run.add_argument("--probe-arg", action="append", default=[], help="extra latency_probe arg")
    run.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    run.add_argument("--git-ref", default="main")
    run.add_argument("--user-data", default=str(DEFAULT_USER_DATA))
    run.add_argument("--ssm-timeout", type=int, default=900)
    run.add_argument("--probe-timeout", type=int, default=1800)
    run.add_argument("--out-dir", default="results")

    clean = sub.add_parser("cleanup", help="terminate expired or orphaned gacha instances")
    clean.add_argument("--execute", action="store_true", help="actually terminate (default: list)")
    clean.add_argument("--run-id", help="target one run regardless of ExpiresAt")
    clean.add_argument("--include-kept", action="store_true", help="also terminate Keep=true")
    args = parser.parse_args(argv)
    if args.command == "run":
        args.subnet = parse_subnets(args.subnet)
    return args


def make_session(args: argparse.Namespace):
    import boto3

    return boto3.Session(profile_name=args.profile, region_name=args.region)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.command == "cleanup":
        ec2 = make_session(args).client("ec2")
        targets = find_cleanup_targets(ec2, utcnow(), args.run_id, args.include_kept)
        for item in targets:
            print(f"{item['instance_id']}  run={item['run_id']}  {item['reason']}")
        if not targets:
            print("nothing to clean up")
        elif args.execute:
            ec2.terminate_instances(InstanceIds=[item["instance_id"] for item in targets])
            print(f"terminated {len(targets)} instance(s)")
        else:
            print("dry-run: add --execute to terminate")
        return 0

    try:
        plan = build_plan(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({**asdict(plan), "total": plan.total}, indent=1))
    if not args.execute:
        if not Path(args.user_data).is_file():
            print(f"warning: user data template not found: {args.user_data}")
        if not args.instance_profile:
            print("warning: --instance-profile is required with --execute")
        print("probe commands:")
        print("\n".join(probe_commands(plan.markets, args.samples, args.stream_seconds,
                                       args.probe_arg)))
        print("dry-run: nothing launched; add --execute to launch")
        return 0
    if not args.instance_profile:
        print("error: --instance-profile is required with --execute", file=sys.stderr)
        return 2
    session = make_session(args)
    gacha = Gacha(session.client("ec2"), session.client("ssm"), plan, args)
    gacha.run()
    return 0 if gacha.kept or plan.keep == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
