import datetime as dt
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ec2_gacha
from test_scoring import encode_line, make_report

NOW = dt.datetime(2026, 11, 25, 8, 0, tzinfo=dt.timezone.utc)
TEMPLATE = "#!/bin/bash\nREPO=__REPO_URL__\nREF=__GIT_REF__\nTTL=__TTL_MINUTES__\n"


class FakeEC2:
    def __init__(self, fail_azs=()):
        self.fail_azs = set(fail_azs)
        self.counter = 0
        self.az_of = {}
        self.run_calls = []
        self.terminated = []
        self.created_tags = []
        self.deleted_tags = []

    def describe_availability_zones(self, ZoneNames):
        return {"AvailabilityZones": [{"ZoneName": name, "ZoneId": f"apne1-az{index}"}
                                      for index, name in enumerate(ZoneNames)]}

    def describe_subnets(self, Filters):
        az = next(item["Values"][0] for item in Filters if item["Name"] == "availability-zone")
        return {"Subnets": [{"SubnetId": f"subnet-{az}"}]}

    def run_instances(self, **params):
        self.run_calls.append(params)
        az = params["SubnetId"][len("subnet-"):]
        if az in self.fail_azs:
            raise RuntimeError("Unsupported instance type in this AZ")
        instances = []
        for _ in range(params["MaxCount"]):
            self.counter += 1
            iid = f"i-{self.counter:04d}"
            self.az_of[iid] = az
            instances.append({"InstanceId": iid})
        return {"Instances": instances}

    def get_waiter(self, name):
        return SimpleNamespace(wait=lambda **kwargs: None)

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [
            {"InstanceId": iid, "Placement": {"AvailabilityZone": self.az_of[iid]}}
            for iid in InstanceIds]}]}

    def create_tags(self, Resources, Tags):
        self.created_tags.append((Resources, Tags))

    def delete_tags(self, Resources, Tags):
        self.deleted_tags.append((Resources, Tags))

    def terminate_instances(self, InstanceIds):
        self.terminated.extend(InstanceIds)


class FakeSSM:
    def __init__(self, outputs, offline=()):
        self.outputs = outputs
        self.offline = set(offline)
        self.commands = {}
        self.sent = []

    def get_parameter(self, Name):
        return {"Parameter": {"Value": "ami-test"}}

    def describe_instance_information(self, Filters):
        ids = Filters[0]["Values"]
        return {"InstanceInformationList": [{"InstanceId": iid, "PingStatus": "Online"}
                                            for iid in ids if iid not in self.offline]}

    def send_command(self, InstanceIds, Parameters, **kwargs):
        command_id = f"cmd-{len(self.commands) + 1}"
        self.commands[command_id] = Parameters["commands"]
        self.sent.append((list(InstanceIds), Parameters["commands"]))
        return {"Command": {"CommandId": command_id}}

    def get_command_invocation(self, CommandId, InstanceId):
        if self.commands[CommandId] == ["shutdown -c"]:
            stdout = ""
        else:
            stdout = self.outputs.get(InstanceId, "")
        return {"Status": "Success", "StandardOutputContent": stdout, "StandardErrorContent": ""}


class BrokenSSM(FakeSSM):
    def send_command(self, **kwargs):
        raise RuntimeError("AccessDenied")


def probe_output(futures_p50, spot_p50):
    return "\n".join([
        "CHRONY System time : 0.000001 seconds fast",
        encode_line("futures", make_report("futures", [("10.0.0.1", futures_p50, futures_p50 * 2, 200)])),
        encode_line("spot", make_report("spot", [("10.0.0.2", spot_p50, spot_p50 * 2, 200)])),
    ])


class GachaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        template = Path(self.tmp.name) / "bootstrap.sh"
        template.write_text(TEMPLATE, encoding="utf-8")
        self.argv = ["run", "--execute", "--az", "zone-a", "--az", "zone-b",
                     "--instance-profile", "probe-profile", "--user-data", str(template),
                     "--out-dir", str(Path(self.tmp.name) / "results")]

    def make(self, ec2, ssm, extra=()):
        args = ec2_gacha.parse_args(self.argv + list(extra))
        plan = ec2_gacha.build_plan(args, now=NOW)
        logs = []
        gacha = ec2_gacha.Gacha(ec2, ssm, plan, args, log=logs.append, sleep=lambda s: None,
                                clock=itertools.count(0, 100).__next__, now=lambda: NOW)
        return gacha, logs

    def test_keeps_best_and_terminates_rest(self):
        ec2 = FakeEC2()
        ssm = FakeSSM({"i-0001": probe_output(900, 1000), "i-0002": probe_output(500, 600)})
        gacha, _ = self.make(ec2, ssm)
        summary = gacha.run()
        self.assertEqual(gacha.kept, ["i-0002"])
        self.assertEqual(ec2.terminated, ["i-0001"])
        self.assertIn((["i-0002"], ["shutdown -c"]), ssm.sent)
        self.assertEqual(ec2.deleted_tags, [(["i-0002"], [{"Key": "ExpiresAt"}])])
        tags = {tag["Key"]: tag["Value"]
                for tag in ec2.run_calls[0]["TagSpecifications"][0]["Tags"]}
        self.assertEqual(tags["ExpiresAt"], "2026-11-25T09:00:00Z")
        self.assertEqual(ec2.run_calls[0]["InstanceInitiatedShutdownBehavior"], "terminate")
        self.assertIn("TTL=60", ec2.run_calls[0]["UserData"])
        best = next(item for item in summary["instances"] if item["instance_id"] == "i-0002")
        self.assertEqual(best["az_id"], "apne1-az1")
        self.assertEqual(best["markets"]["futures"]["p50_us"], 500)
        run_dir = Path(self.tmp.name) / "results" / gacha.plan.run_id
        self.assertTrue((run_dir / "i-0001_spot.json").is_file())
        saved = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["kept"], ["i-0002"])

    def test_failure_terminates_everything(self):
        ec2 = FakeEC2()
        gacha, _ = self.make(ec2, BrokenSSM({}))
        with self.assertRaises(RuntimeError):
            gacha.run()
        self.assertEqual(sorted(ec2.terminated), ["i-0001", "i-0002"])
        self.assertEqual(gacha.kept, [])

    def test_launch_failure_in_one_az_continues(self):
        ec2 = FakeEC2(fail_azs={"zone-a"})
        gacha, _ = self.make(ec2, FakeSSM({"i-0001": probe_output(500, 600)}))
        summary = gacha.run()
        self.assertIn("zone-a", summary["launch_errors"])
        self.assertEqual(gacha.kept, ["i-0001"])
        self.assertEqual(ec2.terminated, [])

    def test_offline_instance_fails_and_is_terminated(self):
        ec2 = FakeEC2()
        ssm = FakeSSM({"i-0001": probe_output(100, 100), "i-0002": probe_output(500, 600)},
                      offline={"i-0001"})
        gacha, _ = self.make(ec2, ssm)
        summary = gacha.run()
        self.assertEqual(gacha.kept, ["i-0002"])
        self.assertEqual(ec2.terminated, ["i-0001"])
        offline = next(item for item in summary["instances"] if item["instance_id"] == "i-0001")
        self.assertIn("SSM offline", offline["reason"])

    def test_threshold_rejects_all(self):
        ec2 = FakeEC2()
        ssm = FakeSSM({"i-0001": probe_output(900, 1000), "i-0002": probe_output(500, 600)})
        gacha, _ = self.make(ec2, ssm, ["--max-p50-us-futures", "100"])
        gacha.run()
        self.assertEqual(gacha.kept, [])
        self.assertEqual(sorted(ec2.terminated), ["i-0001", "i-0002"])


class CleanupEC2:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def describe_instances(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


def instance(iid, **tags):
    return {"InstanceId": iid, "Tags": [{"Key": key, "Value": value}
                                        for key, value in tags.items()]}


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.pages = [
            {"Reservations": [{"Instances": [
                instance("i-expired", GachaRun="r1", ExpiresAt="2026-11-25T07:59:00Z"),
                instance("i-fresh", GachaRun="r2", ExpiresAt="2026-11-25T09:00:00Z"),
            ]}], "NextToken": "page-2"},
            {"Reservations": [{"Instances": [
                instance("i-kept", GachaRun="r1", Keep="true"),
                instance("i-orphan", GachaRun="r3"),
                instance("i-garbled", GachaRun="r3", ExpiresAt="tomorrow"),
            ]}]},
        ]

    def ids(self, **kwargs):
        ec2 = CleanupEC2(self.pages)
        targets = ec2_gacha.find_cleanup_targets(ec2, NOW, **kwargs)
        return ec2, [item["instance_id"] for item in targets]

    def test_expired_and_orphans_only(self):
        ec2, ids = self.ids()
        self.assertEqual(ids, ["i-expired", "i-orphan", "i-garbled"])
        self.assertEqual(ec2.calls[1]["NextToken"], "page-2")

    def test_include_kept(self):
        _, ids = self.ids(include_kept=True)
        self.assertIn("i-kept", ids)
        self.assertNotIn("i-fresh", ids)

    def test_run_id_ignores_expiry_but_spares_kept(self):
        ec2, ids = self.ids(run_id="r2")
        self.assertIn({"Name": "tag:GachaRun", "Values": ["r2"]}, ec2.calls[0]["Filters"])
        self.assertNotIn("i-kept", ids)
        self.assertIn("i-fresh", ids)


class PlanTest(unittest.TestCase):
    def test_rejects_too_many_instances(self):
        args = ec2_gacha.parse_args(["run", "--per-az", "2", "--max-total", "4"])
        with self.assertRaises(ValueError):
            ec2_gacha.build_plan(args)

    def test_rejects_unknown_market(self):
        args = ec2_gacha.parse_args(["run", "--markets", "futures,options"])
        with self.assertRaises(ValueError):
            ec2_gacha.build_plan(args)

    def test_render_rejects_unsafe_ref(self):
        with self.assertRaises(ValueError):
            ec2_gacha.render_user_data(TEMPLATE, "https://x/y.git", "main;rm -rf /", 60)

    def test_dry_run_needs_no_aws(self):
        self.assertEqual(ec2_gacha.main(["run", "--az", "ap-northeast-1a"]), 0)


if __name__ == "__main__":
    unittest.main()
