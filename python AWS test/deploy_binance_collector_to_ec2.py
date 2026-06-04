#!/usr/bin/env python3
"""
Build and deploy the Binance C++ collector to a fresh Ubuntu EC2 instance.

Default flow:
  1. Compile the C++ source into a Linux binary using WSL.
  2. Upload the binary to a private S3 bucket.
  3. Launch an Ubuntu 22.04 EC2 instance.
  4. Download the binary during first boot and run it as a systemd service.

Use --mode ec2-source if you want the instance to compile the source itself.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.request import urlopen

from botocore.config import Config
from botocore.exceptions import ClientError


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_SOURCE = REPO_ROOT / "binance_klines_collector_cpp20.cpp"
DEFAULT_BUILD_DIR = SCRIPT_DIR / "build"
DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-west-2")
DEFAULT_SERVICE_NAME = "binance-collector"
DEFAULT_BINARY_NAME = "binance_klines_collector"
DEFAULT_INSTANCE_TYPE = "t2.nano"
DEFAULT_SSH_KEY_NAME = "binance-collector-key"
DEFAULT_SSH_KEY_PATH = SCRIPT_DIR / "keys" / f"{DEFAULT_SSH_KEY_NAME}.pem"

# Optional hardcoded AWS credentials.
#
# Leave these blank to use the normal AWS credential chain:
#   aws configure, ~/.aws/credentials, environment variables, IAM roles, etc.
#
# If you really want hardcoded credentials, paste them here locally.
# Do not share this file after adding real keys.
HARDCODED_AWS_ACCESS_KEY_ID = ""
HARDCODED_AWS_SECRET_ACCESS_KEY = ""
HARDCODED_AWS_SESSION_TOKEN = ""  # Only needed for temporary/STS credentials.
HARDCODED_AWS_REGION = ""  # Optional override, for example "eu-west-2".

# Edit this block if you want to run this file with no command-line args.
#
# Common choices:
#   CONFIG_COMPILE_ONLY = True
#       Compile the Linux binary on this PC, then stop.
#   CONFIG_CONFIRM_AWS_ACTIONS = True
#       Allow the script to launch/terminate paid AWS resources.
#   CONFIG_ENABLE_SSH = True
#       Create/reuse an EC2 SSH key and SSH security group automatically.
#   CONFIG_SSH_SECURITY_GROUP_ID = "sg-..."
#       Use an existing inbound/SSH security group.
CONFIG_MODE = "wsl-binary"
CONFIG_SOURCE = DEFAULT_SOURCE
CONFIG_BUILD_DIR = DEFAULT_BUILD_DIR
CONFIG_BINARY_NAME = DEFAULT_BINARY_NAME
CONFIG_SERVICE_NAME = DEFAULT_SERVICE_NAME
CONFIG_PROGRAM_ARGS: list[str] = []
CONFIG_REGION = DEFAULT_REGION
CONFIG_AMI_ID = ""
CONFIG_INSTANCE_TYPE = DEFAULT_INSTANCE_TYPE
CONFIG_VOLUME_GB = 80
CONFIG_SUBNET_ID = ""
CONFIG_SECURITY_GROUP_ID = ""
CONFIG_SSH_SECURITY_GROUP_ID = ""
CONFIG_INBOUND_SECURITY_GROUP_ID = ""
CONFIG_ENABLE_SSH = True
CONFIG_KEY_NAME = DEFAULT_SSH_KEY_NAME
CONFIG_KEY_PATH = DEFAULT_SSH_KEY_PATH
CONFIG_SSH_CIDR = ""  # Empty means auto-detect this PC's public IP and use /32.
CONFIG_AUTHORIZE_SSH = True
CONFIG_REPLACE_SSH_KEY = False
CONFIG_OPEN_SSH_AFTER_LAUNCH = True
CONFIG_SSH_SAME_WINDOW = False
CONFIG_SSH_TIMEOUT_SECONDS = 180
CONFIG_SSH_USER = "ubuntu"
CONFIG_FETCH_LOGS_AFTER_LAUNCH = True
CONFIG_LOG_TARGET = "bootstrap"  # "bootstrap", "service", or "both".
CONFIG_LOG_LINES = 200
CONFIG_FOLLOW_LOGS = True
CONFIG_BOOTSTRAP_LOG_TIMEOUT_SECONDS = 600
CONFIG_PRINT_LAST_BIN_ROW_AFTER_LOGS = True
CONFIG_REMOTE_DATA_DIR = "/opt/binance-collector/data_dump"
CONFIG_DATA_POLL_SECONDS = 10
CONFIG_IAM_INSTANCE_PROFILE = ""
CONFIG_BUCKET = ""
CONFIG_PRESIGN_HOURS = 6
CONFIG_COMPILE_ONLY = False
CONFIG_SKIP_BUILD = False
CONFIG_BINARY_PATH = Path("")
CONFIG_CONFIRM_AWS_ACTIONS = True
CONFIG_TERMINATE_INSTANCE_ID = ""
CONFIG_NO_WAIT = False

WSL_DEV_INSTALL_COMMAND = (
    'wsl.exe bash -lc "sudo apt update && sudo apt install -y '
    "build-essential pkg-config libcurl4-openssl-dev libssl-dev "
    "libboost-dev libboost-system-dev nlohmann-json3-dev "
    'libpugixml-dev libzip-dev"'
)

UBUNTU_2204_AMI_SSM_PARAMETERS = (
    "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
    "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id",
)

RUNTIME_PACKAGES_UBUNTU_2204 = (
    "ca-certificates",
    "curl",
    "libcurl4",
    "libssl3",
    "libboost-system1.74.0",
    "libpugixml1v5",
    "libzip4",
)

BUILD_PACKAGES_UBUNTU_2204 = (
    "build-essential",
    "pkg-config",
    "ca-certificates",
    "curl",
    "libcurl4-openssl-dev",
    "libssl-dev",
    "libboost-dev",
    "libboost-system-dev",
    "nlohmann-json3-dev",
    "libpugixml-dev",
    "libzip-dev",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile, upload, launch EC2, and run the Binance C++ collector."
    )
    parser.add_argument(
        "--mode",
        choices=("wsl-binary", "ec2-source"),
        default=CONFIG_MODE,
        help="wsl-binary compiles locally in WSL; ec2-source uploads source and builds on EC2.",
    )
    parser.add_argument("--source", type=Path, default=CONFIG_SOURCE)
    parser.add_argument("--build-dir", type=Path, default=CONFIG_BUILD_DIR)
    parser.add_argument("--binary-name", default=CONFIG_BINARY_NAME)
    parser.add_argument("--service-name", default=CONFIG_SERVICE_NAME)
    parser.add_argument(
        "--program-arg",
        action="append",
        default=list(CONFIG_PROGRAM_ARGS),
        help="Argument passed to the collector service. Repeat for multiple args.",
    )
    parser.add_argument("--region", default=CONFIG_REGION)
    parser.add_argument("--ami-id", default=CONFIG_AMI_ID or None, help="Use a specific Ubuntu 22.04 x86_64 AMI ID.")
    parser.add_argument("--instance-type", default=CONFIG_INSTANCE_TYPE)
    parser.add_argument("--volume-gb", type=int, default=CONFIG_VOLUME_GB)
    parser.add_argument("--subnet-id", default=CONFIG_SUBNET_ID or None)
    parser.add_argument(
        "--security-group-id",
        default=CONFIG_SECURITY_GROUP_ID or None,
        help="Existing security group to attach to the instance.",
    )
    parser.add_argument(
        "--ssh-security-group-id",
        default=CONFIG_SSH_SECURITY_GROUP_ID or None,
        help="Alias for --security-group-id when the group already allows SSH/inbound access.",
    )
    parser.add_argument(
        "--inbound-security-group-id",
        default=CONFIG_INBOUND_SECURITY_GROUP_ID or None,
        help="Alias for --security-group-id when using a custom inbound-access group.",
    )
    parser.add_argument("--no-ssh", action="store_true", default=not CONFIG_ENABLE_SSH, help="Disable automatic SSH key/security-group setup.")
    parser.add_argument("--key-name", default=CONFIG_KEY_NAME or None, help="Optional EC2 key pair name for SSH access.")
    parser.add_argument("--key-path", type=Path, default=CONFIG_KEY_PATH, help="Local private key path for SSH.")
    parser.add_argument("--ssh-cidr", default=CONFIG_SSH_CIDR or None, help="CIDR/IP allowed for SSH. Empty means current public IP /32.")
    parser.add_argument("--no-authorize-ssh", action="store_true", default=not CONFIG_AUTHORIZE_SSH, help="Do not add an SSH ingress rule.")
    parser.add_argument("--replace-ssh-key", action="store_true", default=CONFIG_REPLACE_SSH_KEY, help="Delete/recreate the AWS key pair if needed.")
    parser.add_argument("--no-open-ssh", action="store_true", default=not CONFIG_OPEN_SSH_AFTER_LAUNCH, help="Do not open an SSH terminal after launch.")
    parser.add_argument("--ssh-same-window", action="store_true", default=CONFIG_SSH_SAME_WINDOW, help="Run SSH in this terminal instead of a new one.")
    parser.add_argument("--ssh-timeout", type=int, default=CONFIG_SSH_TIMEOUT_SECONDS)
    parser.add_argument("--ssh-user", default=CONFIG_SSH_USER)
    parser.add_argument("--no-fetch-logs", action="store_true", default=not CONFIG_FETCH_LOGS_AFTER_LAUNCH, help="Do not fetch logs after launch.")
    parser.add_argument("--log-target", choices=("bootstrap", "service", "both"), default=CONFIG_LOG_TARGET)
    parser.add_argument("--log-lines", type=int, default=CONFIG_LOG_LINES)
    parser.add_argument("--no-follow-logs", action="store_true", default=not CONFIG_FOLLOW_LOGS, help="Print recent logs once instead of following.")
    parser.add_argument("--bootstrap-log-timeout", type=int, default=CONFIG_BOOTSTRAP_LOG_TIMEOUT_SECONDS)
    parser.add_argument("--no-print-bin-row", action="store_true", default=not CONFIG_PRINT_LAST_BIN_ROW_AFTER_LOGS, help="Do not print the latest .bin row after logs.")
    parser.add_argument("--remote-data-dir", default=CONFIG_REMOTE_DATA_DIR)
    parser.add_argument("--data-poll-seconds", type=int, default=CONFIG_DATA_POLL_SECONDS)
    parser.add_argument(
        "--iam-instance-profile",
        default=CONFIG_IAM_INSTANCE_PROFILE or None,
        help="Optional instance profile name or ARN. Not required for the default presigned URL flow.",
    )
    parser.add_argument(
        "--bucket",
        default=CONFIG_BUCKET or None,
        help="S3 bucket for the temporary upload. Default: ec2-linux-program-deploy-ACCOUNT-REGION.",
    )
    parser.add_argument("--presign-hours", type=int, default=CONFIG_PRESIGN_HOURS)
    parser.add_argument(
        "--compile-only",
        action="store_true",
        default=CONFIG_COMPILE_ONLY,
        help="Compile the Linux binary with WSL and stop before touching AWS.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        default=CONFIG_SKIP_BUILD,
        help="Use an existing Linux binary from --binary-path instead of compiling.",
    )
    parser.add_argument(
        "--binary-path",
        type=Path,
        default=CONFIG_BINARY_PATH if str(CONFIG_BINARY_PATH) else None,
        help="Existing Linux binary to upload when --skip-build is used.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=CONFIG_CONFIRM_AWS_ACTIONS,
        help="Actually launch EC2. Required unless --compile-only is used.",
    )
    parser.add_argument(
        "--terminate-instance",
        default=CONFIG_TERMINATE_INSTANCE_ID or None,
        help="Terminate an EC2 instance ID using the configured AWS credentials, then exit.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        default=CONFIG_NO_WAIT,
        help="Do not wait for the EC2 instance to reach the running state.",
    )
    return parser.parse_args()


def require_boto3() -> tuple[Any, Any]:
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing Python AWS dependency. Run:\n"
            "  python -m pip install -r requirements.txt"
        ) from exc
    return boto3, ClientError


def run_process(command: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def fail_from_process(message: str, process: subprocess.CompletedProcess[str]) -> None:
    print(message, file=sys.stderr)
    if process.stdout.strip():
        print("\nstdout:\n" + process.stdout.strip(), file=sys.stderr)
    if process.stderr.strip():
        print("\nstderr:\n" + process.stderr.strip(), file=sys.stderr)
    raise SystemExit(process.returncode or 1)


def validate_safe_name(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", value):
        raise SystemExit(f"{label} must contain only letters, numbers, dot, underscore, at, or hyphen.")
    return value


def ensure_source_exists(source: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"C++ source file was not found: {source}")
    return source


def windows_to_wsl_path(path: Path) -> str:
    process = run_process(["wsl.exe", "wslpath", "-a", str(path)], timeout=30)
    if process.returncode != 0:
        fail_from_process("Could not convert Windows path to WSL path.", process)
    return process.stdout.strip()


def ensure_wsl_ready() -> None:
    process = run_process(["wsl.exe", "bash", "-lc", "echo WSL_OK"], timeout=30)
    if process.returncode != 0 or "WSL_OK" not in process.stdout:
        fail_from_process("WSL is not ready. Start Ubuntu once, then retry.", process)


def ensure_wsl_build_dependencies() -> None:
    check_script = r"""
set +e
missing=0
check_cmd() {
  command -v "\$1" >/dev/null 2>&1 || { echo "missing command: \$1"; missing=1; }
}
check_file() {
  test -e "\$1" || { echo "missing header: \$1 (\$2)"; missing=1; }
}
check_pkg() {
  pkg-config --exists "\$1" 2>/dev/null || { echo "missing pkg-config package: \$1 (\$2)"; missing=1; }
}
check_cmd g++
check_cmd pkg-config
check_file /usr/include/boost/beast/websocket.hpp libboost-dev
check_file /usr/include/nlohmann/json.hpp nlohmann-json3-dev
check_pkg libcurl libcurl4-openssl-dev
check_pkg openssl libssl-dev
check_pkg pugixml libpugixml-dev
check_pkg libzip libzip-dev
ldconfig -p 2>/dev/null | grep -q 'libboost_system' || { echo "missing library: libboost_system (libboost-system-dev)"; missing=1; }
exit "\$missing"
"""
    process = run_process(["wsl.exe", "bash", "-lc", check_script], timeout=30)
    if process.returncode != 0:
        print("WSL is missing one or more Linux C++ build dependencies.", file=sys.stderr)
        if process.stdout.strip():
            print("\nDetected problems:\n" + process.stdout.strip(), file=sys.stderr)
        print("\nInstall them from PowerShell with:", file=sys.stderr)
        print(f"  {WSL_DEV_INSTALL_COMMAND}", file=sys.stderr)
        raise SystemExit(1)


def compile_with_wsl(source: Path, build_dir: Path, binary_name: str) -> Path:
    ensure_wsl_ready()
    ensure_wsl_build_dependencies()

    build_dir = build_dir.expanduser().resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    binary_path = build_dir / binary_name

    source_wsl = windows_to_wsl_path(source)
    output_wsl = windows_to_wsl_path(binary_path)
    output_dir_wsl = shlex.quote(str(PurePosixPath(output_wsl).parent))

    compile_script = f"""
set -euo pipefail
mkdir -p {output_dir_wsl}
g++ -std=c++20 -O3 -DNDEBUG -pipe -pthread \
  {shlex.quote(source_wsl)} \
  -o {shlex.quote(output_wsl)} \
  $(pkg-config --cflags --libs libcurl openssl pugixml libzip) \
  -lboost_system
strip {shlex.quote(output_wsl)} || true
chmod +x {shlex.quote(output_wsl)}
file {shlex.quote(output_wsl)}
"""
    print(f"Compiling Linux binary with WSL: {binary_path}")
    process = run_process(["wsl.exe", "bash", "-lc", compile_script], timeout=20 * 60)
    if process.returncode != 0:
        fail_from_process("Linux compile failed.", process)
    print(process.stdout.strip())
    return binary_path


def ensure_existing_binary(binary_path: Path) -> Path:
    binary_path = binary_path.expanduser().resolve()
    if not binary_path.is_file():
        raise SystemExit(f"Linux binary was not found: {binary_path}")

    process = run_process(
        ["wsl.exe", "bash", "-lc", f"file {shlex.quote(windows_to_wsl_path(binary_path))}"],
        timeout=30,
    )
    if process.returncode == 0:
        print(process.stdout.strip())
        if "ELF" not in process.stdout:
            raise SystemExit(
                f"{binary_path} does not look like a Linux ELF binary. "
                "Do not upload the Windows .exe."
            )
    else:
        print("Warning: could not verify binary with WSL file command.", file=sys.stderr)
    return binary_path


def create_session(region: str) -> Any:
    boto3, _client_error = require_boto3()
    session_region = HARDCODED_AWS_REGION.strip() or region

    access_key_id = HARDCODED_AWS_ACCESS_KEY_ID.strip()
    secret_access_key = HARDCODED_AWS_SECRET_ACCESS_KEY.strip()
    session_token = HARDCODED_AWS_SESSION_TOKEN.strip()

    if bool(access_key_id) != bool(secret_access_key):
        raise SystemExit(
            "Hardcoded AWS credentials are incomplete. Set both "
            "HARDCODED_AWS_ACCESS_KEY_ID and HARDCODED_AWS_SECRET_ACCESS_KEY, "
            "or leave both blank."
        )

    if access_key_id and secret_access_key:
        print("Using hardcoded AWS credentials from deploy_binance_collector_to_ec2.py")
        session_kwargs: dict[str, str] = {
            "region_name": session_region,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
        }
        if session_token:
            session_kwargs["aws_session_token"] = session_token
        return boto3.Session(**session_kwargs)

    return boto3.Session(region_name=session_region)


def default_bucket_name(session: Any, region: str) -> str:
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    return f"ec2-linux-program-deploy-{account_id}-{region}".lower()


def ensure_bucket(session: Any, bucket: str, region: str) -> None:
    _boto3, ClientError = require_boto3()
    s3 = s3_client(session, region)
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code")
        if status == 403:
            raise SystemExit(f"S3 bucket exists but is not accessible to you: {bucket}") from exc
        if status not in (301, 404) and code not in ("404", "NoSuchBucket", "NotFound"):
            raise

    print(f"Creating private S3 bucket: {bucket}")
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )

    try:
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
    except ClientError as exc:
        print(f"Warning: could not set public access block on {bucket}: {exc}", file=sys.stderr)


def upload_and_presign(
    session: Any,
    bucket: str,
    artifact_path: Path,
    object_prefix: str,
    expires_seconds: int,
    region: str,
) -> tuple[str, str]:
    s3 = s3_client(session, region)
    key = f"{object_prefix}/{uuid.uuid4()}-{artifact_path.name}"
    print(f"Uploading artifact to s3://{bucket}/{key}")
    s3.upload_file(str(artifact_path), bucket, key)
    presigned_url = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_seconds,
    )
    return key, presigned_url


def s3_client(session: Any, region: str) -> Any:
    return session.client(
        "s3",
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def get_latest_ubuntu_2204_ami(session: Any) -> str:
    _boto3, ClientError = require_boto3()
    ssm = session.client("ssm")
    last_error: Exception | None = None
    for parameter_name in UBUNTU_2204_AMI_SSM_PARAMETERS:
        try:
            response = ssm.get_parameter(Name=parameter_name)
            return response["Parameter"]["Value"]
        except ClientError as exc:
            last_error = exc
    raise SystemExit(
        "Could not resolve the latest Ubuntu 22.04 AMI from SSM. "
        "Pass --ami-id with an Ubuntu 22.04 x86_64 AMI."
    ) from last_error


def get_default_subnet(session: Any) -> tuple[str, str]:
    ec2 = session.client("ec2")
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise SystemExit("No default VPC found. Pass --subnet-id and --security-group-id.")

    vpc_id = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "default-for-az", "Values": ["true"]},
        ]
    )["Subnets"]
    if not subnets:
        raise SystemExit("No default subnet found. Pass --subnet-id.")

    subnets.sort(key=lambda item: item.get("AvailableIpAddressCount", 0), reverse=True)
    return vpc_id, subnets[0]["SubnetId"]


def vpc_for_subnet(session: Any, subnet_id: str) -> str:
    ec2 = session.client("ec2")
    subnets = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"]
    if not subnets:
        raise SystemExit(f"Subnet not found: {subnet_id}")
    return subnets[0]["VpcId"]


def ensure_security_group(session: Any, vpc_id: str, service_name: str) -> str:
    ec2 = session.client("ec2")
    group_name = f"{service_name}-no-inbound"
    groups = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [group_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if groups:
        return groups[0]["GroupId"]

    response = ec2.create_security_group(
        GroupName=group_name,
        Description=f"No-inbound security group for {service_name}",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [{"Key": "Name", "Value": group_name}],
            }
        ],
    )
    return response["GroupId"]


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


def validate_security_group(session: Any, group_id: str, vpc_id: str) -> str:
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


def ensure_ssh_security_group(session: Any, vpc_id: str, service_name: str, cidr: str, authorize: bool) -> str:
    ec2 = session.client("ec2")
    group_name = f"{service_name}-ssh"
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
            Description=f"SSH security group for {service_name}",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": "Name", "Value": group_name}],
                }
            ],
        )
        group_id = response["GroupId"]

    if authorize:
        authorize_ssh_ingress(session, group_id, cidr)
    return group_id


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


def ensure_ssh_key_pair(session: Any, key_name: str, key_path: Path, replace_key: bool) -> Path:
    if not key_name:
        raise SystemExit("SSH is enabled but no key name is set.")
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
            f"AWS key pair {key_name!r} exists, but the local private key is missing:\n"
            f"  {key_path}\n"
            "AWS cannot show the private key again. Use a new CONFIG_KEY_NAME, "
            "or set CONFIG_REPLACE_SSH_KEY = True if no existing instance needs that key."
        )

    print(f"Creating AWS SSH key pair: {key_name}")
    response = ec2.create_key_pair(KeyName=key_name, KeyType="rsa")
    key_path.write_text(response["KeyMaterial"], encoding="utf-8")
    lock_down_key_file(key_path)
    return key_path


def wait_for_ssh(public_ip: str, timeout_seconds: int) -> bool:
    print(f"Waiting for SSH on {public_ip}:22")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((public_ip, 22), timeout=5):
                print("SSH port is open.")
                return True
        except OSError:
            time.sleep(5)
    print(f"SSH did not open within {timeout_seconds} seconds.")
    return False


def ssh_command(key_path: Path, user: str, public_ip: str) -> list[str]:
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
    command = ssh_command(key_path, user, public_ip)
    command_line = powershell_command_from_args(command)
    print("SSH command:")
    print("  " + command_line)

    if same_window:
        raise SystemExit(subprocess.run(command, text=True).returncode)

    if shutil.which("wt.exe"):
        subprocess.Popen(["wt.exe", "powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", command_line])
    else:
        subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", command_line])


def build_remote_log_command(service_name: str, target: str, lines: int, follow: bool) -> str:
    lines = max(1, min(lines, 10_000))
    bootstrap_path = f"/var/log/{service_name}-bootstrap.log"
    tail_flags = f"-n {lines}" + (" -f" if follow else "")

    if target == "bootstrap":
        inner = f"touch {shlex.quote(bootstrap_path)}; tail {tail_flags} {shlex.quote(bootstrap_path)}"
        return "sudo bash -lc " + shlex.quote(inner)

    if target == "service":
        if follow:
            return f"sudo journalctl -u {shlex.quote(service_name)} -f"
        return f"sudo journalctl -u {shlex.quote(service_name)} --no-pager -n {lines}"

    if target == "both":
        if follow:
            inner = (
                f"touch {shlex.quote(bootstrap_path)}; "
                "echo '===== bootstrap log ====='; "
                f"tail -n {lines} -f {shlex.quote(bootstrap_path)} & "
                "bootstrap_pid=$!; "
                "echo '===== service log ====='; "
                f"journalctl -u {shlex.quote(service_name)} -f & "
                "service_pid=$!; "
                "trap 'kill $bootstrap_pid $service_pid 2>/dev/null || true' INT TERM EXIT; "
                "wait"
            )
            return "sudo bash -lc " + shlex.quote(inner)

        inner = (
            f"touch {shlex.quote(bootstrap_path)}; "
            "echo '===== bootstrap log ====='; "
            f"tail -n {lines} {shlex.quote(bootstrap_path)}; "
            "echo; "
            "echo '===== service log ====='; "
            f"journalctl -u {shlex.quote(service_name)} --no-pager -n {lines}"
        )
        return "sudo bash -lc " + shlex.quote(inner)

    raise SystemExit(f"Unknown log target: {target}")


def fetch_logs_over_ssh(
    key_path: Path,
    user: str,
    public_ip: str,
    service_name: str,
    target: str,
    lines: int,
    follow: bool,
) -> int:
    remote_command = build_remote_log_command(service_name, target, lines, follow)
    command = ssh_command(key_path, user, public_ip) + [remote_command]
    print("")
    print(f"Fetching {target} logs over SSH. Press Ctrl+C to stop." if follow else f"Fetching {target} logs over SSH.")
    print("Log command:")
    print("  " + subprocess.list2cmdline(command))
    try:
        return subprocess.run(command, text=True).returncode
    except KeyboardInterrupt:
        print("")
        print("Stopped following logs.")
        return 130


def wait_for_bootstrap_done_over_ssh(
    key_path: Path,
    user: str,
    public_ip: str,
    service_name: str,
    lines: int,
    timeout_seconds: int,
) -> int:
    bootstrap_path = f"/var/log/{service_name}-bootstrap.log"
    code = f"""
import os
import sys
import time

path = {bootstrap_path!r}
lines = {max(1, min(lines, 10_000))}
timeout = {max(1, timeout_seconds)}
deadline = time.time() + timeout

while not os.path.exists(path):
    if time.time() > deadline:
        print(f"Timed out waiting for {{path}}", flush=True)
        raise SystemExit(124)
    time.sleep(1)

with open(path, "r", errors="replace") as handle:
    history = handle.readlines()
    for line in history[-lines:]:
        print(line, end="")
    sys.stdout.flush()
    if any("BOOTSTRAP_DONE" in line for line in history):
        raise SystemExit(0)
    position = handle.tell()

    while True:
        if time.time() > deadline:
            print("\\nTimed out waiting for BOOTSTRAP_DONE", flush=True)
            raise SystemExit(124)
        handle.seek(position)
        line = handle.readline()
        if not line:
            time.sleep(1)
            continue
        position = handle.tell()
        print(line, end="")
        sys.stdout.flush()
        if "BOOTSTRAP_DONE" in line:
            raise SystemExit(0)
"""
    remote_command = "sudo python3 -c " + shlex.quote(code)
    command = ssh_command(key_path, user, public_ip) + [remote_command]
    print("")
    print("Fetching bootstrap logs until setup finishes.")
    try:
        return subprocess.run(command, text=True).returncode
    except KeyboardInterrupt:
        print("")
        print("Stopped following bootstrap logs.")
        return 130


def poll_latest_bin_row_over_ssh(
    key_path: Path,
    user: str,
    public_ip: str,
    data_dir: str,
    poll_seconds: int,
) -> int:
    poll_seconds = max(1, poll_seconds)
    code = f"""
import datetime as dt
import glob
import os
import struct
import sys
import time

data_dir = {data_dir!r}
poll_seconds = {poll_seconds}
record = struct.Struct("<24sqqddddddqdddd")
last_printed = None

def utc_ms(value):
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).isoformat()

def latest_bin_file():
    paths = glob.glob(os.path.join(data_dir, "daily_bin", "**", "*.bin"), recursive=True)
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return None
    return max(paths, key=lambda p: (os.path.getmtime(p), p))

print(f"Watching latest .bin row under {{data_dir}} every {{poll_seconds}}s", flush=True)
while True:
    path = latest_bin_file()
    if not path:
        print("No .bin files found yet.", flush=True)
        time.sleep(poll_seconds)
        continue

    size = os.path.getsize(path)
    if size < record.size:
        print(f"Latest .bin has no complete records yet: {{path}} size={{size}}", flush=True)
        time.sleep(poll_seconds)
        continue

    usable_size = size - (size % record.size)
    row_id = (path, usable_size)
    try:
        with open(path, "rb") as handle:
            handle.seek(usable_size - record.size)
            values = record.unpack(handle.read(record.size))
    except Exception as exc:
        print(f"Could not read {{path}}: {{exc}}", flush=True)
        time.sleep(poll_seconds)
        continue

    symbol = values[0].split(b"\\0", 1)[0].decode("ascii", errors="replace")
    open_time, close_time = values[1], values[2]
    open_, high, low, close, volume, quote_volume = values[3:9]
    trades = values[9]
    rows = usable_size // record.size
    changed = "" if row_id == last_printed else " NEW"
    print(
        f"{{dt.datetime.now(dt.timezone.utc).isoformat()}} latest{{changed}} "
        f"rows={{rows}} file={{path}} "
        f"open={{utc_ms(open_time)}} symbol={{symbol}} "
        f"O={{open_}} H={{high}} L={{low}} C={{close}} V={{volume}} trades={{trades}}",
        flush=True,
    )
    last_printed = row_id
    time.sleep(poll_seconds)
"""
    remote_command = "sudo python3 -c " + shlex.quote(code)
    command = ssh_command(key_path, user, public_ip) + [remote_command]
    print("")
    print(f"Printing latest .bin row every {poll_seconds} seconds. Press Ctrl+C to stop.")
    try:
        return subprocess.run(command, text=True).returncode
    except KeyboardInterrupt:
        print("")
        print("Stopped printing latest .bin row.")
        return 130


def shell_join_args(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def service_unit(service_name: str, binary_name: str, program_args: list[str]) -> str:
    exec_start = f"/opt/{service_name}/{binary_name}"
    if program_args:
        exec_start += " " + shell_join_args(program_args)
    return f"""[Unit]
Description=Binance C++ collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=collector
Group=collector
WorkingDirectory=/opt/{service_name}
ExecStart={exec_start}
Restart=always
RestartSec=5
LimitNOFILE=1048576
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def build_binary_user_data(
    download_url: str,
    service_name: str,
    binary_name: str,
    program_args: list[str],
) -> str:
    runtime_packages = " ".join(RUNTIME_PACKAGES_UBUNTU_2204)
    unit = service_unit(service_name, binary_name, program_args)
    return f"""#!/bin/bash
exec > >(tee -a /var/log/{service_name}-bootstrap.log | logger -t {service_name}-bootstrap -s 2>/dev/console) 2>&1
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y {runtime_packages}

SERVICE_NAME={shlex.quote(service_name)}
BINARY_NAME={shlex.quote(binary_name)}
SERVICE_USER=collector
APP_HOME="/opt/$SERVICE_NAME"
APP_BIN="$APP_HOME/$BINARY_NAME"

id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$APP_HOME"

echo "Downloading collector binary"
curl -fL --retry 8 --retry-delay 5 {shlex.quote(download_url)} -o "$APP_BIN"
chmod 0755 "$APP_BIN"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_HOME"

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<'SERVICE_EOF'
{unit}SERVICE_EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" || true
echo "BOOTSTRAP_DONE"
"""


def build_source_user_data(
    download_url: str,
    service_name: str,
    binary_name: str,
    source_name: str,
    program_args: list[str],
) -> str:
    build_packages = " ".join(BUILD_PACKAGES_UBUNTU_2204)
    unit = service_unit(service_name, binary_name, program_args)
    return f"""#!/bin/bash
exec > >(tee -a /var/log/{service_name}-bootstrap.log | logger -t {service_name}-bootstrap -s 2>/dev/console) 2>&1
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y {build_packages}

SERVICE_NAME={shlex.quote(service_name)}
BINARY_NAME={shlex.quote(binary_name)}
SOURCE_NAME={shlex.quote(source_name)}
SERVICE_USER=collector
APP_HOME="/opt/$SERVICE_NAME"
SOURCE_DIR="$APP_HOME/source"
APP_BIN="$APP_HOME/$BINARY_NAME"
SOURCE_PATH="$SOURCE_DIR/$SOURCE_NAME"

id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$SOURCE_DIR"

echo "Downloading collector source"
curl -fL --retry 8 --retry-delay 5 {shlex.quote(download_url)} -o "$SOURCE_PATH"
echo "Compiling collector source"
g++ -std=c++20 -O3 -DNDEBUG -pipe -pthread "$SOURCE_PATH" -o "$APP_BIN" $(pkg-config --cflags --libs libcurl openssl pugixml libzip) -lboost_system
strip "$APP_BIN" || true
chmod 0755 "$APP_BIN"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_HOME"

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<'SERVICE_EOF'
{unit}SERVICE_EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" || true
echo "BOOTSTRAP_DONE"
"""


def launch_instance(
    session: Any,
    *,
    ami_id: str,
    instance_type: str,
    subnet_id: str,
    security_group_id: str,
    volume_gb: int,
    user_data: str,
    service_name: str,
    key_name: str | None,
    iam_instance_profile: str | None,
) -> str:
    ec2 = session.client("ec2")
    image = ec2.describe_images(ImageIds=[ami_id])["Images"][0]
    root_device_name = image.get("RootDeviceName", "/dev/sda1")
    run_args: dict[str, Any] = {
        "ImageId": ami_id,
        "InstanceType": instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        "UserData": user_data,
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "SubnetId": subnet_id,
                "Groups": [security_group_id],
                "AssociatePublicIpAddress": True,
            }
        ],
        "BlockDeviceMappings": [
            {
                "DeviceName": root_device_name,
                "Ebs": {
                    "VolumeSize": volume_gb,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }
        ],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": service_name},
                    {"Key": "CreatedBy", "Value": "deploy_binance_collector_to_ec2.py"},
                ],
            },
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "Name", "Value": service_name},
                    {"Key": "CreatedBy", "Value": "deploy_binance_collector_to_ec2.py"},
                ],
            },
        ],
    }
    if key_name:
        run_args["KeyName"] = key_name
    if iam_instance_profile:
        profile_key = "Arn" if iam_instance_profile.startswith("arn:") else "Name"
        run_args["IamInstanceProfile"] = {profile_key: iam_instance_profile}

    response = ec2.run_instances(**run_args)
    return response["Instances"][0]["InstanceId"]


def describe_instance(session: Any, instance_id: str) -> dict[str, Any]:
    ec2 = session.client("ec2")
    response = ec2.describe_instances(InstanceIds=[instance_id])
    return response["Reservations"][0]["Instances"][0]


def wait_until_running(session: Any, instance_id: str) -> dict[str, Any]:
    ec2 = session.client("ec2")
    print(f"Waiting for instance to run: {instance_id}")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    return describe_instance(session, instance_id)


def terminate_instance(session: Any, instance_id: str) -> None:
    if not re.fullmatch(r"i-[0-9a-fA-F]+", instance_id):
        raise SystemExit(f"Invalid EC2 instance ID: {instance_id}")
    ec2 = session.client("ec2")
    ec2.terminate_instances(InstanceIds=[instance_id])
    print(f"Terminate requested for instance: {instance_id}")


def main() -> int:
    args = parse_args()
    service_name = validate_safe_name(args.service_name, "--service-name")
    binary_name = validate_safe_name(args.binary_name, "--binary-name")

    if args.terminate_instance:
        if not args.yes:
            raise SystemExit("Refusing to terminate an instance without --yes.")
        session = create_session(args.region)
        terminate_instance(session, args.terminate_instance)
        return 0

    source = ensure_source_exists(args.source)

    if args.compile_only and args.mode != "wsl-binary":
        raise SystemExit("--compile-only only works with --mode wsl-binary.")
    if args.skip_build and not args.binary_path:
        raise SystemExit("--skip-build requires --binary-path.")
    if args.skip_build and args.mode != "wsl-binary":
        raise SystemExit("--skip-build is only valid with --mode wsl-binary.")
    if not args.compile_only and not args.yes:
        raise SystemExit(
            "Refusing to launch a paid EC2 instance without --yes.\n"
            "To build only, run: python deploy_binance_collector_to_ec2.py --compile-only\n"
            "To launch, run: python deploy_binance_collector_to_ec2.py --yes"
        )

    if args.mode == "wsl-binary":
        artifact_path = (
            ensure_existing_binary(args.binary_path)
            if args.skip_build
            else compile_with_wsl(source, args.build_dir, binary_name)
        )
        object_prefix = "ec2-program-binaries"
    else:
        artifact_path = source
        object_prefix = "ec2-program-sources"

    if args.compile_only:
        print(f"Linux binary ready: {artifact_path}")
        return 0

    session = create_session(args.region)
    bucket = args.bucket or default_bucket_name(session, args.region)
    ensure_bucket(session, bucket, args.region)

    key, presigned_url = upload_and_presign(
        session,
        bucket,
        artifact_path,
        object_prefix,
        expires_seconds=args.presign_hours * 60 * 60,
        region=args.region,
    )

    ami_id = args.ami_id or get_latest_ubuntu_2204_ami(session)
    supplied_security_groups = [
        value
        for value in (
            args.security_group_id,
            args.ssh_security_group_id,
            args.inbound_security_group_id,
        )
        if value
    ]
    if len(set(supplied_security_groups)) > 1:
        raise SystemExit(
            "Pass only one security group option, or pass the same group ID to each alias."
        )
    supplied_security_group_id = supplied_security_groups[0] if supplied_security_groups else None
    ssh_enabled = not args.no_ssh

    if args.subnet_id:
        subnet_id = args.subnet_id
        vpc_id = vpc_for_subnet(session, subnet_id)
    else:
        vpc_id, subnet_id = get_default_subnet(session)

    ssh_key_path: Path | None = None
    launch_key_name: str | None = None
    if ssh_enabled:
        ssh_key_path = ensure_ssh_key_pair(session, args.key_name, args.key_path, args.replace_ssh_key)
        launch_key_name = args.key_name
        if supplied_security_group_id:
            security_group_id = validate_security_group(session, supplied_security_group_id, vpc_id)
            if not args.no_authorize_ssh:
                authorize_ssh_ingress(session, security_group_id, normalize_ssh_cidr(args.ssh_cidr))
            else:
                print(f"Using existing security group without changes: {security_group_id}")
        else:
            security_group_id = ensure_ssh_security_group(
                session,
                vpc_id,
                service_name,
                normalize_ssh_cidr(args.ssh_cidr),
                authorize=not args.no_authorize_ssh,
            )
    else:
        launch_key_name = None
        security_group_id = supplied_security_group_id or ensure_security_group(session, vpc_id, service_name)

    if args.mode == "wsl-binary":
        user_data = build_binary_user_data(
            presigned_url,
            service_name,
            binary_name,
            args.program_arg,
        )
    else:
        user_data = build_source_user_data(
            presigned_url,
            service_name,
            binary_name,
            source.name,
            args.program_arg,
        )

    print("Launching EC2 instance")
    print(f"  region: {args.region}")
    print(f"  ami: {ami_id}")
    print(f"  instance type: {args.instance_type}")
    print(f"  subnet: {subnet_id}")
    print(f"  security group: {security_group_id}")
    if ssh_enabled:
        print(f"  ssh key pair: {launch_key_name}")
        print(f"  ssh private key: {ssh_key_path}")
    print(f"  upload: s3://{bucket}/{key}")
    instance_id = launch_instance(
        session,
        ami_id=ami_id,
        instance_type=args.instance_type,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        volume_gb=args.volume_gb,
        user_data=user_data,
        service_name=service_name,
        key_name=launch_key_name,
        iam_instance_profile=args.iam_instance_profile,
    )

    print(f"Started instance: {instance_id}")
    if args.no_wait:
        return 0

    instance = wait_until_running(session, instance_id)
    public_ip = instance.get("PublicIpAddress", "(no public IP yet)")
    private_ip = instance.get("PrivateIpAddress", "(no private IP yet)")
    print("Instance is running.")
    print(f"  instance id: {instance_id}")
    print(f"  public ip: {public_ip}")
    print(f"  private ip: {private_ip}")
    print(f"  service: {service_name}.service")
    print("")
    print("The first-boot script is still installing/running in the background.")
    print("On the instance, logs are written to:")
    print(f"  /var/log/{service_name}-bootstrap.log")
    print(f"  journalctl -u {service_name} -f")
    if ssh_enabled and ssh_key_path and public_ip != "(no public IP yet)":
        print("")
        command = ssh_command(ssh_key_path, args.ssh_user, public_ip)
        print("SSH is enabled. Connect with:")
        print("  " + powershell_command_from_args(command))

        ssh_ready = wait_for_ssh(public_ip, args.ssh_timeout)
        if ssh_ready and not args.no_open_ssh:
            open_ssh_terminal(ssh_key_path, args.ssh_user, public_ip, args.ssh_same_window)
        if ssh_ready and not args.no_fetch_logs:
            if args.log_target == "bootstrap" and not args.no_follow_logs and not args.no_print_bin_row:
                wait_for_bootstrap_done_over_ssh(
                    ssh_key_path,
                    args.ssh_user,
                    public_ip,
                    service_name,
                    args.log_lines,
                    args.bootstrap_log_timeout,
                )
            else:
                fetch_logs_over_ssh(
                    ssh_key_path,
                    args.ssh_user,
                    public_ip,
                    service_name,
                    args.log_target,
                    args.log_lines,
                    follow=not args.no_follow_logs,
                )
        if ssh_ready and not args.no_print_bin_row:
            poll_latest_bin_row_over_ssh(
                ssh_key_path,
                args.ssh_user,
                public_ip,
                args.remote_data_dir,
                args.data_poll_seconds,
            )
    time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
