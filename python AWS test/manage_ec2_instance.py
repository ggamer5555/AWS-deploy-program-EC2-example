#!/usr/bin/env python3
"""
Start, stop, reboot, hibernate, terminate, or inspect EC2 instances.

Normal use:
  1. Edit the CONFIG_... variables below.
  2. Run: python .\\manage_ec2_instance.py

Command-line overrides are also available:
  python .\\manage_ec2_instance.py --action status --instance-id i-...
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any


DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-west-2")
VALID_ACTIONS = ("status", "start", "stop", "reboot", "hibernate", "terminate")

# Optional hardcoded AWS credentials.
#
# Leave blank to use the normal AWS credential chain:
# aws configure, environment variables, ~/.aws/credentials, IAM roles, etc.
# Do not commit real keys to GitHub.
HARDCODED_AWS_ACCESS_KEY_ID = ""
HARDCODED_AWS_SECRET_ACCESS_KEY = ""
HARDCODED_AWS_SESSION_TOKEN = ""
HARDCODED_AWS_REGION = ""

# Edit these inputs if you want to run the script with no command-line args.
CONFIG_ACTION = "status"  # status, start, stop, reboot, hibernate, terminate
CONFIG_INSTANCE_IDS: list[str] = []
CONFIG_INSTANCE_NAME = ""  # Optional Name tag lookup if CONFIG_INSTANCE_IDS is empty.
CONFIG_REGION = DEFAULT_REGION
CONFIG_CONFIRM_ACTIONS = False  # Required for start/stop/reboot/hibernate/terminate.
CONFIG_WAIT = True
CONFIG_FORCE_STOP = False
CONFIG_DRY_RUN = False
CONFIG_SHOW_PUBLIC_IP = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage EC2 instance lifecycle state.")
    parser.add_argument("--action", choices=VALID_ACTIONS, default=CONFIG_ACTION)
    parser.add_argument(
        "--instance-id",
        action="append",
        dest="instance_ids",
        default=list(CONFIG_INSTANCE_IDS),
        help="EC2 instance ID. Repeat for multiple instances.",
    )
    parser.add_argument(
        "--name",
        default=CONFIG_INSTANCE_NAME or None,
        help="Find running/stopped instances by exact EC2 Name tag when no instance ID is supplied.",
    )
    parser.add_argument("--region", default=CONFIG_REGION)
    parser.add_argument("--yes", action="store_true", default=CONFIG_CONFIRM_ACTIONS)
    parser.add_argument("--no-wait", action="store_true", default=not CONFIG_WAIT)
    parser.add_argument("--force-stop", action="store_true", default=CONFIG_FORCE_STOP)
    parser.add_argument("--dry-run", action="store_true", default=CONFIG_DRY_RUN)
    parser.add_argument("--no-public-ip", action="store_true", default=not CONFIG_SHOW_PUBLIC_IP)
    return parser.parse_args()


def require_boto3() -> Any:
    try:
        import boto3
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing boto3. Install dependencies with:\n"
            "  python -m pip install -r requirements.txt"
        ) from exc
    return boto3


def create_session(region: str) -> Any:
    boto3 = require_boto3()
    session_region = HARDCODED_AWS_REGION.strip() or region
    access_key_id = HARDCODED_AWS_ACCESS_KEY_ID.strip()
    secret_access_key = HARDCODED_AWS_SECRET_ACCESS_KEY.strip()
    session_token = HARDCODED_AWS_SESSION_TOKEN.strip()

    if bool(access_key_id) != bool(secret_access_key):
        raise SystemExit(
            "Hardcoded AWS credentials are incomplete. Set both access key and secret key, "
            "or leave both blank."
        )

    if access_key_id and secret_access_key:
        kwargs: dict[str, str] = {
            "region_name": session_region,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
        }
        if session_token:
            kwargs["aws_session_token"] = session_token
        return boto3.Session(**kwargs)

    return boto3.Session(region_name=session_region)


def flatten_instances(response: dict[str, Any]) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for reservation in response.get("Reservations", []):
        instances.extend(reservation.get("Instances", []))
    return instances


def instance_name(instance: dict[str, Any]) -> str:
    for tag in instance.get("Tags", []):
        if tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


def find_instances_by_name(ec2: Any, name: str) -> list[str]:
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [name]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )
    instances = flatten_instances(response)
    instances.sort(key=lambda item: item["LaunchTime"], reverse=True)
    return [instance["InstanceId"] for instance in instances]


def resolve_instance_ids(ec2: Any, ids: list[str], name: str | None) -> list[str]:
    cleaned = [item.strip() for item in ids if item and item.strip()]
    if cleaned:
        return cleaned
    if name:
        found = find_instances_by_name(ec2, name)
        if found:
            return found
        raise SystemExit(f"No pending/running/stopping/stopped instances found with Name={name!r}.")
    raise SystemExit("No instances supplied. Set CONFIG_INSTANCE_IDS or CONFIG_INSTANCE_NAME.")


def print_status(ec2: Any, instance_ids: list[str], show_public_ip: bool) -> None:
    response = ec2.describe_instances(InstanceIds=instance_ids)
    instances = flatten_instances(response)
    if not instances:
        print("No instances found.")
        return

    print(
        "InstanceId".ljust(22),
        "State".ljust(14),
        "Type".ljust(12),
        "Name".ljust(28),
        "PublicIP".ljust(16) if show_public_ip else "",
        "LaunchTime",
    )
    for instance in instances:
        public_ip = instance.get("PublicIpAddress", "") if show_public_ip else ""
        print(
            instance["InstanceId"].ljust(22),
            instance["State"]["Name"].ljust(14),
            instance.get("InstanceType", "").ljust(12),
            instance_name(instance).ljust(28),
            public_ip.ljust(16) if show_public_ip else "",
            instance["LaunchTime"].isoformat(),
        )


def wait_for_action(ec2: Any, action: str, instance_ids: list[str]) -> None:
    waiter_name_by_action = {
        "start": "instance_running",
        "stop": "instance_stopped",
        "hibernate": "instance_stopped",
        "terminate": "instance_terminated",
    }
    waiter_name = waiter_name_by_action.get(action)
    if not waiter_name:
        return
    print(f"Waiting for {waiter_name.replace('_', ' ')}...")
    ec2.get_waiter(waiter_name).wait(InstanceIds=instance_ids)


def run_action(ec2: Any, action: str, instance_ids: list[str], args: argparse.Namespace) -> None:
    if action == "status":
        print_status(ec2, instance_ids, show_public_ip=not args.no_public_ip)
        return

    if not args.yes:
        raise SystemExit(
            f"Refusing to {action} EC2 instances without confirmation.\n"
            "Set CONFIG_CONFIRM_ACTIONS = True or pass --yes."
        )

    print(f"Action: {action}")
    print("Instances: " + ", ".join(instance_ids))

    if action == "start":
        ec2.start_instances(InstanceIds=instance_ids, DryRun=args.dry_run)
    elif action == "stop":
        ec2.stop_instances(InstanceIds=instance_ids, Force=args.force_stop, DryRun=args.dry_run)
    elif action == "hibernate":
        ec2.stop_instances(InstanceIds=instance_ids, Hibernate=True, DryRun=args.dry_run)
    elif action == "reboot":
        ec2.reboot_instances(InstanceIds=instance_ids, DryRun=args.dry_run)
    elif action == "terminate":
        ec2.terminate_instances(InstanceIds=instance_ids, DryRun=args.dry_run)
    else:
        raise SystemExit(f"Unsupported action: {action}")

    if args.dry_run:
        print("Dry run request sent.")
        return

    print(f"{action.capitalize()} request sent.")
    if not args.no_wait:
        wait_for_action(ec2, action, instance_ids)
        print_status(ec2, instance_ids, show_public_ip=not args.no_public_ip)


def main() -> int:
    args = parse_args()
    session = create_session(args.region)
    ec2 = session.client("ec2")
    instance_ids = resolve_instance_ids(ec2, args.instance_ids, args.name)
    run_action(ec2, args.action, instance_ids, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
