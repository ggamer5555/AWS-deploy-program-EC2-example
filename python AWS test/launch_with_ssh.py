#!/usr/bin/env python3
"""
Launch a new Binance collector EC2 instance with SSH set up automatically.

This creates/reuses an EC2 key pair, saves the private key locally, opens port
22 only to your current public IP, launches the collector with the local Linux
binary, then opens an SSH terminal.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from botocore.exceptions import ClientError

import deploy_binance_collector_to_ec2 as deploy


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_KEY_NAME = "binance-collector-key"
DEFAULT_KEY_PATH = SCRIPT_DIR / "keys" / f"{DEFAULT_KEY_NAME}.pem"
DEFAULT_BINARY_PATH = SCRIPT_DIR / "build" / deploy.DEFAULT_BINARY_NAME

# Edit this block if you want to run this file with no command-line args.
CONFIG_REGION = deploy.DEFAULT_REGION
CONFIG_INSTANCE_TYPE = deploy.DEFAULT_INSTANCE_TYPE
CONFIG_VOLUME_GB = 80
CONFIG_SUBNET_ID = ""
CONFIG_SECURITY_GROUP_ID = ""
CONFIG_SSH_SECURITY_GROUP_ID = ""
CONFIG_SSH_CIDR = ""  # Empty means auto-detect this PC's public IP and use /32.
CONFIG_NO_AUTHORIZE_SSH = False
CONFIG_KEY_NAME = DEFAULT_KEY_NAME
CONFIG_KEY_PATH = DEFAULT_KEY_PATH
CONFIG_BINARY_PATH = DEFAULT_BINARY_PATH
CONFIG_REBUILD = False
CONFIG_REPLACE_KEY = False
CONFIG_NO_CONNECT = False
CONFIG_SAME_WINDOW = False
CONFIG_SSH_TIMEOUT = 180
CONFIG_USER = "ubuntu"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch/connect to a Binance collector EC2 instance with SSH.")
    parser.add_argument("--region", default=CONFIG_REGION)
    parser.add_argument("--instance-type", default=CONFIG_INSTANCE_TYPE)
    parser.add_argument("--volume-gb", type=int, default=CONFIG_VOLUME_GB)
    parser.add_argument("--subnet-id", default=CONFIG_SUBNET_ID or None)
    parser.add_argument(
        "--security-group-id",
        default=CONFIG_SECURITY_GROUP_ID or None,
        help="Existing security group to attach. The script can add the SSH rule unless --no-authorize-ssh is used.",
    )
    parser.add_argument(
        "--ssh-security-group-id",
        default=CONFIG_SSH_SECURITY_GROUP_ID or None,
        help="Alias for --security-group-id when passing an existing SSH group.",
    )
    parser.add_argument(
        "--ssh-cidr",
        default=CONFIG_SSH_CIDR or None,
        help="CIDR/IP allowed for SSH. Default: your current public IP as /32.",
    )
    parser.add_argument(
        "--no-authorize-ssh",
        action="store_true",
        default=CONFIG_NO_AUTHORIZE_SSH,
        help="Do not add a port 22 ingress rule. Use this if your security group already allows SSH.",
    )
    parser.add_argument("--key-name", default=CONFIG_KEY_NAME)
    parser.add_argument("--key-path", type=Path, default=CONFIG_KEY_PATH)
    parser.add_argument("--binary-path", type=Path, default=CONFIG_BINARY_PATH)
    parser.add_argument("--rebuild", action="store_true", default=CONFIG_REBUILD, help="Force rebuild before launch.")
    parser.add_argument(
        "--replace-key",
        action="store_true",
        default=CONFIG_REPLACE_KEY,
        help="Delete/recreate the AWS key pair if needed.",
    )
    parser.add_argument("--no-connect", action="store_true", default=CONFIG_NO_CONNECT, help="Launch only; do not open SSH.")
    parser.add_argument(
        "--same-window",
        action="store_true",
        default=CONFIG_SAME_WINDOW,
        help="Run SSH in the current terminal instead of a new window.",
    )
    parser.add_argument("--ssh-timeout", type=int, default=CONFIG_SSH_TIMEOUT)
    parser.add_argument("--user", default=CONFIG_USER)
    return parser.parse_args()


def current_public_ip() -> str:
    with urlopen("https://checkip.amazonaws.com", timeout=20) as response:
        ip = response.read().decode("utf-8").strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip):
        raise SystemExit(f"Could not detect a valid public IPv4 address: {ip!r}")
    return ip


def normalize_ssh_cidr(value: str | None) -> str:
    if not value:
        return current_public_ip() + "/32"
    value = value.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value):
        return value + "/32"
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}", value):
        return value
    raise SystemExit("--ssh-cidr must be an IPv4 address like 1.2.3.4 or CIDR like 1.2.3.4/32.")


def key_pair_exists(ec2: Any, key_name: str) -> bool:
    try:
        ec2.describe_key_pairs(KeyNames=[key_name])
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "InvalidKeyPair.NotFound":
            return False
        raise


def lock_down_key_file(key_path: Path) -> None:
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        username = os.environ.get("USERNAME")
        if username:
            subprocess.run(
                ["icacls", str(key_path), "/inheritance:r", "/grant:r", f"{username}:R"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )


def ensure_key_pair(session: Any, key_name: str, key_path: Path, replace_key: bool) -> Path:
    ec2 = session.client("ec2")
    key_path = key_path.expanduser().resolve()
    key_path.parent.mkdir(parents=True, exist_ok=True)

    exists_in_aws = key_pair_exists(ec2, key_name)
    exists_locally = key_path.is_file()

    if replace_key and exists_in_aws:
        print(f"Deleting existing AWS key pair: {key_name}")
        ec2.delete_key_pair(KeyName=key_name)
        exists_in_aws = False

    if exists_in_aws and exists_locally:
        lock_down_key_file(key_path)
        print(f"Using existing SSH key pair: {key_name}")
        return key_path

    if exists_in_aws and not exists_locally:
        raise SystemExit(
            f"AWS key pair {key_name!r} exists, but the private key file is missing:\n"
            f"  {key_path}\n"
            "AWS cannot show the private key again. Use --key-name with a new name, "
            "or run with --replace-key if no existing instance needs that key."
        )

    if exists_locally and not replace_key:
        raise SystemExit(
            f"Local key file exists but AWS key pair {key_name!r} does not:\n"
            f"  {key_path}\n"
            "Use --key-name with a new name or --replace-key."
        )

    print(f"Creating AWS SSH key pair: {key_name}")
    response = ec2.create_key_pair(KeyName=key_name, KeyType="rsa")
    key_path.write_text(response["KeyMaterial"], encoding="utf-8")
    lock_down_key_file(key_path)
    return key_path


def authorize_ssh_ingress(session: Any, group_id: str, cidr: str) -> None:
    ec2 = session.client("ec2")
    try:
        ec2.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": cidr, "Description": "SSH access"}],
                }
            ],
        )
        print(f"Allowed SSH from {cidr} on {group_id}")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "InvalidPermission.Duplicate":
            raise
        print(f"SSH from {cidr} already allowed on {group_id}")


def ensure_ssh_security_group(session: Any, vpc_id: str, cidr: str, authorize_ssh: bool) -> str:
    ec2 = session.client("ec2")
    group_name = "binance-collector-ssh"
    groups = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [group_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if groups:
        group_id = groups[0]["GroupId"]
    else:
        response = ec2.create_security_group(
            GroupName=group_name,
            Description="SSH from current IP for Binance collector",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": "Name", "Value": group_name}],
                }
            ],
        )
        group_id = response["GroupId"]

    if authorize_ssh:
        authorize_ssh_ingress(session, group_id, cidr)

    return group_id


def validate_existing_security_group(session: Any, group_id: str, vpc_id: str) -> str:
    if not re.fullmatch(r"sg-[0-9a-fA-F]+", group_id):
        raise SystemExit(f"Invalid security group ID: {group_id}")
    ec2 = session.client("ec2")
    groups = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"]
    if not groups:
        raise SystemExit(f"Security group not found: {group_id}")
    group_vpc_id = groups[0].get("VpcId")
    if group_vpc_id != vpc_id:
        raise SystemExit(f"Security group {group_id} is in VPC {group_vpc_id}, but subnet is in VPC {vpc_id}.")
    return group_id


def binary_is_current(binary_path: Path) -> bool:
    source = deploy.DEFAULT_SOURCE
    return binary_path.is_file() and binary_path.stat().st_mtime >= source.stat().st_mtime


def run_deployer(args: argparse.Namespace, subnet_id: str, security_group_id: str, key_path: Path) -> str:
    deployer = SCRIPT_DIR / "deploy_binance_collector_to_ec2.py"
    command = [
        sys.executable,
        str(deployer),
        "--yes",
        "--region",
        args.region,
        "--instance-type",
        args.instance_type,
        "--volume-gb",
        str(args.volume_gb),
        "--subnet-id",
        subnet_id,
        "--security-group-id",
        security_group_id,
        "--key-name",
        args.key_name,
    ]

    binary_path = args.binary_path.expanduser().resolve()
    if binary_is_current(binary_path) and not args.rebuild:
        command += ["--skip-build", "--binary-path", str(binary_path)]

    instance_id = ""
    process = subprocess.Popen(
        command,
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        match = re.search(r"Started instance:\s+(i-[0-9a-fA-F]+)", line)
        if match:
            instance_id = match.group(1)
    return_code = process.wait()
    if return_code != 0:
        raise SystemExit(return_code)
    if not instance_id:
        raise SystemExit("Could not read instance ID from deployer output.")
    return instance_id


def wait_for_ssh(public_ip: str, timeout_seconds: int) -> None:
    print(f"Waiting for SSH on {public_ip}:22")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((public_ip, 22), timeout=5):
                print("SSH port is open.")
                return
        except OSError:
            time.sleep(5)
    raise SystemExit(f"SSH did not open within {timeout_seconds} seconds.")


def ssh_base_command(key_path: Path, user: str, public_ip: str) -> list[str]:
    known_hosts_path = Path.home() / ".ssh" / "binance_collector_known_hosts"
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    return [
        "ssh",
        "-i",
        str(key_path),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        f"{user}@{public_ip}",
    ]


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_command_from_args(command: list[str]) -> str:
    return "& " + " ".join(powershell_quote(str(part)) for part in command)


def open_ssh_terminal(key_path: Path, user: str, public_ip: str, same_window: bool) -> None:
    command = ssh_base_command(key_path, user, public_ip)
    command_line = powershell_command_from_args(command)
    print("SSH command:")
    print("  " + command_line)

    if same_window:
        raise SystemExit(subprocess.run(command, text=True).returncode)

    if shutil.which("wt.exe"):
        subprocess.Popen(["wt.exe", "powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", command_line])
    else:
        subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", command_line])


def main() -> int:
    args = parse_args()
    session = deploy.create_session(args.region)
    key_path = ensure_key_pair(session, args.key_name, args.key_path, args.replace_key)

    if args.subnet_id:
        subnet_id = args.subnet_id
        vpc_id = deploy.vpc_for_subnet(session, subnet_id)
    else:
        vpc_id, subnet_id = deploy.get_default_subnet(session)

    supplied_security_groups = [
        value for value in (args.security_group_id, args.ssh_security_group_id) if value
    ]
    if len(set(supplied_security_groups)) > 1:
        raise SystemExit("Pass only one of --security-group-id or --ssh-security-group-id.")

    cidr = normalize_ssh_cidr(args.ssh_cidr)
    authorize_ssh = not args.no_authorize_ssh
    if supplied_security_groups:
        security_group_id = validate_existing_security_group(session, supplied_security_groups[0], vpc_id)
        if authorize_ssh:
            authorize_ssh_ingress(session, security_group_id, cidr)
        else:
            print(f"Using existing security group without changes: {security_group_id}")
    else:
        security_group_id = ensure_ssh_security_group(session, vpc_id, cidr, authorize_ssh)
    instance_id = run_deployer(args, subnet_id, security_group_id, key_path)

    instance = deploy.describe_instance(session, instance_id)
    public_ip = instance.get("PublicIpAddress")
    if not public_ip:
        raise SystemExit(f"Instance {instance_id} has no public IP.")

    print(f"Instance ready for SSH: {instance_id} ({public_ip})")
    if not args.no_connect:
        wait_for_ssh(public_ip, args.ssh_timeout)
        open_ssh_terminal(key_path, args.user, public_ip, args.same_window)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
