#!/usr/bin/env python3
"""
Fetch EC2/bootstrap/service logs for the Binance collector instance.

Console logs work without SSH. Service logs need an instance launched with an
SSH key, such as one created by launch_with_ssh.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

import deploy_binance_collector_to_ec2 as deploy


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_KEY_PATH = SCRIPT_DIR / "keys" / "binance-collector-key.pem"

# Edit this block if you want to run this file with no command-line args.
CONFIG_INSTANCE_ID = ""  # Empty means newest Name=binance-collector instance.
CONFIG_NAME = deploy.DEFAULT_SERVICE_NAME
CONFIG_REGION = deploy.DEFAULT_REGION
CONFIG_LINES = 200
CONFIG_RAW = False
CONFIG_CONSOLE = False
CONFIG_SERVICE = False
CONFIG_BOOTSTRAP = False
CONFIG_CLOUD_INIT = False
CONFIG_FOLLOW = False
CONFIG_KEY_PATH = DEFAULT_KEY_PATH
CONFIG_USER = "ubuntu"
CONFIG_LIST_DATA = False
CONFIG_DOWNLOAD_DATA = False
CONFIG_REMOTE_DATA_DIR = "/opt/binance-collector/data_dump"
CONFIG_LOCAL_DOWNLOAD_DIR = SCRIPT_DIR / "downloaded_data"
CONFIG_EXTRACT_DOWNLOAD = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch logs from a Binance collector EC2 instance.")
    parser.add_argument(
        "--instance-id",
        default=CONFIG_INSTANCE_ID or None,
        help="EC2 instance ID. If omitted, uses the newest Name=binance-collector instance.",
    )
    parser.add_argument("--name", default=CONFIG_NAME, help="Name tag to search when --instance-id is omitted.")
    parser.add_argument("--region", default=CONFIG_REGION)
    parser.add_argument("--lines", type=int, default=CONFIG_LINES)
    parser.add_argument("--raw", action="store_true", default=CONFIG_RAW, help="Do not redact temporary AWS URLs/keys from output.")
    parser.add_argument("--console", action="store_true", default=CONFIG_CONSOLE, help="Print EC2 console output. This works without SSH.")
    parser.add_argument("--service", action="store_true", default=CONFIG_SERVICE, help="Print systemd journal logs over SSH.")
    parser.add_argument("--bootstrap", action="store_true", default=CONFIG_BOOTSTRAP, help="Print bootstrap log over SSH.")
    parser.add_argument("--cloud-init", action="store_true", default=CONFIG_CLOUD_INIT, help="Print cloud-init output over SSH.")
    parser.add_argument("--follow", action="store_true", default=CONFIG_FOLLOW, help="Follow SSH logs live.")
    parser.add_argument("--key-path", type=Path, default=CONFIG_KEY_PATH)
    parser.add_argument("--user", default=CONFIG_USER)
    parser.add_argument("--list-data", action="store_true", default=CONFIG_LIST_DATA, help="List .bin/.csv/.log files saved by the collector.")
    parser.add_argument("--download-data", action="store_true", default=CONFIG_DOWNLOAD_DATA, help="Download the collector data_dump folder as a .tgz archive.")
    parser.add_argument("--remote-data-dir", default=CONFIG_REMOTE_DATA_DIR)
    parser.add_argument("--local-download-dir", type=Path, default=CONFIG_LOCAL_DOWNLOAD_DIR)
    parser.add_argument("--no-extract", action="store_true", default=not CONFIG_EXTRACT_DOWNLOAD, help="Do not extract downloaded .tgz archive.")
    return parser.parse_args()


def latest_instance_by_name(session: Any, name: str) -> str:
    ec2 = session.client("ec2")
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [name]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )
    instances: list[dict[str, Any]] = []
    for reservation in response["Reservations"]:
        instances.extend(reservation["Instances"])
    if not instances:
        raise SystemExit(f"No EC2 instances found with Name={name!r}. Pass --instance-id.")
    instances.sort(key=lambda instance: instance["LaunchTime"], reverse=True)
    return instances[0]["InstanceId"]


def redact(text: str) -> str:
    text = re.sub(r"https://\S*X-Amz-\S+", "[REDACTED_PRESIGNED_S3_URL]", text)
    text = re.sub(r"AKIA[0-9A-Z]{16}", "[REDACTED_AWS_ACCESS_KEY]", text)
    text = re.sub(r"(?i)(AWSAccessKeyId>)[^<]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(Signature>)[^<]+", r"\1[REDACTED]", text)
    return text


def print_console_log(session: Any, instance_id: str, lines: int, raw: bool) -> None:
    ec2 = session.client("ec2")
    try:
        response = ec2.get_console_output(InstanceId=instance_id, Latest=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UnsupportedOperation":
            raise
        response = ec2.get_console_output(InstanceId=instance_id)
    output = response.get("Output") or ""
    if not raw:
        output = redact(output)
    selected = output.splitlines()[-lines:]
    print("\n".join(selected))


def instance_public_ip(session: Any, instance_id: str) -> str:
    instance = deploy.describe_instance(session, instance_id)
    ip = instance.get("PublicIpAddress")
    if not ip:
        raise SystemExit(f"Instance {instance_id} has no public IP address.")
    return ip


def ssh_command(key_path: Path, user: str, public_ip: str, remote_command: str) -> list[str]:
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
        remote_command,
    ]


def base_ssh_command(key_path: Path, user: str, public_ip: str) -> list[str]:
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


def run_ssh_log(session: Any, instance_id: str, args: argparse.Namespace, remote_command: str) -> None:
    key_path = args.key_path.expanduser().resolve()
    if not key_path.is_file():
        raise SystemExit(f"SSH key file not found: {key_path}")
    public_ip = instance_public_ip(session, instance_id)
    command = ssh_command(key_path, args.user, public_ip, remote_command)
    completed = subprocess.run(command, text=True)
    raise SystemExit(completed.returncode)


def list_data_files(session: Any, instance_id: str, args: argparse.Namespace) -> None:
    key_path = args.key_path.expanduser().resolve()
    if not key_path.is_file():
        raise SystemExit(f"SSH key file not found: {key_path}")
    public_ip = instance_public_ip(session, instance_id)
    remote_dir = shlex.quote(args.remote_data_dir)
    remote = (
        f"sudo test -d {remote_dir} && "
        f"sudo find {remote_dir} -type f "
        r"\( -name '*.bin' -o -name '*.csv' -o -name '*.log' \) "
        r"-printf '%12s  %TY-%Tm-%Td %TH:%TM  %p\n' | sort "
        f"|| echo 'Data directory not found yet: {args.remote_data_dir}'"
    )
    completed = subprocess.run(base_ssh_command(key_path, args.user, public_ip) + [remote], text=True)
    raise SystemExit(completed.returncode)


def download_data_archive(session: Any, instance_id: str, args: argparse.Namespace) -> None:
    key_path = args.key_path.expanduser().resolve()
    if not key_path.is_file():
        raise SystemExit(f"SSH key file not found: {key_path}")
    public_ip = instance_public_ip(session, instance_id)
    local_dir = args.local_download_dir.expanduser().resolve()
    local_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    archive_path = local_dir / f"{instance_id}-data_dump-{stamp}.tgz"
    remote_path = args.remote_data_dir.strip("/")
    if not remote_path:
        raise SystemExit("Refusing to archive root directory.")
    remote = f"sudo tar -C / -czf - {shlex.quote(remote_path)}"
    print(f"Downloading {args.remote_data_dir} to {archive_path}", file=sys.stderr)
    with archive_path.open("wb") as archive:
        completed = subprocess.run(
            base_ssh_command(key_path, args.user, public_ip) + [remote],
            stdout=archive,
            stderr=sys.stderr,
        )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    print(f"Downloaded: {archive_path}")
    if not args.no_extract:
        extract_dir = local_dir / archive_path.stem
        extract_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "r:gz") as tar:
            target_root = extract_dir.resolve()
            for member in tar.getmembers():
                member_path = (extract_dir / member.name).resolve()
                if target_root not in (member_path, *member_path.parents):
                    raise SystemExit(f"Refusing unsafe tar member path: {member.name}")
            tar.extractall(extract_dir)
        print(f"Extracted: {extract_dir}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    args = parse_args()
    session = deploy.create_session(args.region)
    instance_id = args.instance_id or latest_instance_by_name(session, args.name)

    if not any((args.console, args.service, args.bootstrap, args.cloud_init, args.list_data, args.download_data)):
        args.console = True

    print(f"Instance: {instance_id}", file=sys.stderr)

    if args.console:
        print_console_log(session, instance_id, args.lines, args.raw)

    if args.list_data:
        list_data_files(session, instance_id, args)

    if args.download_data:
        download_data_archive(session, instance_id, args)

    if args.bootstrap:
        remote = f"sudo tail -n {args.lines} {'-f' if args.follow else ''} /var/log/{deploy.DEFAULT_SERVICE_NAME}-bootstrap.log"
        run_ssh_log(session, instance_id, args, remote)

    if args.cloud_init:
        remote = f"sudo tail -n {args.lines} {'-f' if args.follow else ''} /var/log/cloud-init-output.log"
        run_ssh_log(session, instance_id, args, remote)

    if args.service:
        if args.follow:
            remote = f"sudo journalctl -u {deploy.DEFAULT_SERVICE_NAME} -f"
        else:
            remote = f"sudo journalctl -u {deploy.DEFAULT_SERVICE_NAME} --no-pager -n {args.lines}"
        run_ssh_log(session, instance_id, args, remote)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
