#!/usr/bin/env python3
"""Resolve the recorded W3 instance using the course AWS safety wrapper."""
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import lab


def recorded_security_group_matches(instance, manifest):
    expected = manifest.get("security_group_id")
    attached = {group.get("GroupId") for group in instance.get("SecurityGroups", [])}
    return isinstance(expected, str) and bool(expected) and attached == {expected}


def main():
    manifest_path = ROOT / ".local" / "resources.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SystemExit("STOP: .local/resources.json is missing or unsafe")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    instance_id = manifest.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.startswith("i-"):
        raise SystemExit("STOP: resources.json must identify one exact instance_id")

    with contextlib.redirect_stdout(sys.stderr):
        ctx = lab.verify()
    response = lab.run_aws(["ec2", "describe-instances", "--instance-ids", instance_id], ctx["region"])
    instances = [instance for reservation in response.get("Reservations", [])
                 for instance in reservation.get("Instances", [])]
    if len(instances) != 1:
        raise SystemExit("STOP: expected exactly one recorded instance")
    instance = instances[0]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if tags.get("course") != lab.COURSE or tags.get("week") != "w03":
        raise SystemExit("STOP: recorded instance is missing the expected course/week tags")
    for name in ("group", "owner"):
        if manifest.get(name) and tags.get(name) != manifest[name]:
            raise SystemExit(f"STOP: instance {name} tag does not match resources.json")
    if instance.get("State", {}).get("Name") != "running":
        raise SystemExit("STOP: recorded instance is not running; no start action was attempted")
    if not recorded_security_group_matches(instance, manifest):
        raise SystemExit("STOP: resources.json security_group_id does not match the instance's attached security group")
    public_ip = instance.get("PublicIpAddress")
    if not public_ip:
        raise SystemExit("STOP: instance has no current public IPv4 address")
    expected_ip = os.environ.get("CURRENT_EGRESS_IP", "")
    try:
        expected_cidr = str(ipaddress.IPv4Network(expected_ip + "/32", strict=True))
    except ipaddress.AddressValueError:
        raise SystemExit("STOP: current Codespace IPv4 was not provided or is invalid")
    security_group_ids = [group["GroupId"] for group in instance.get("SecurityGroups", [])]
    if not security_group_ids:
        raise SystemExit("STOP: instance has no attached security group")
    security_groups = lab.run_aws(
        ["ec2", "describe-security-groups", "--group-ids", *security_group_ids], ctx["region"]
    ).get("SecurityGroups", [])
    permitted = set()
    for group in security_groups:
        for rule in group.get("IpPermissions", []):
            if rule.get("IpProtocol") != "tcp" or rule.get("FromPort") != rule.get("ToPort"):
                raise SystemExit("STOP: attached security group has an unexpected inbound rule")
            port = rule.get("FromPort")
            for source in rule.get("IpRanges", []):
                if source.get("CidrIp") != expected_cidr or port not in {22, 80}:
                    raise SystemExit("STOP: attached security group has an unexpected inbound source or port")
                permitted.add((port, source["CidrIp"]))
            if rule.get("Ipv6Ranges") or rule.get("PrefixListIds") or rule.get("UserIdGroupPairs"):
                raise SystemExit("STOP: attached security group includes a non-IPv4-CIDR inbound source")
    if permitted != {(22, expected_cidr), (80, expected_cidr)}:
        raise SystemExit("STOP: security groups do not allow the current Codespace /32 on TCP 22 and 80")
    key_path = Path(manifest.get("ssh_key_path", "~/.ssh/id_ed25519")).expanduser()
    print(json.dumps({"instance_id": instance_id, "public_ip": public_ip,
                      "key_path": str(key_path), "region": ctx["region"]}))


if __name__ == "__main__":
    main()