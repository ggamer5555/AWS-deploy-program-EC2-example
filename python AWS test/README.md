# Binance C++ Collector AWS Deployer

Build a C++20 Binance kline collector as a Linux binary, launch an Ubuntu EC2 instance, run the collector as a `systemd` service, stream setup logs, and then print the latest row written to the collector `.bin` files.

The main entry point is:

```powershell
python .\deploy_binance_collector_to_ec2.py
```

By default it compiles locally in WSL, uploads the binary through private S3, creates an SSH-enabled `t2.nano` EC2 instance, starts `binance-collector.service`, prints bootstrap logs, then prints the latest `.bin` row every 10 seconds.

## Safety

This project can create paid AWS resources. Stop or terminate instances when finished.

Never commit real AWS credentials, SSH private keys, downloaded data, or temporary archives. `.gitignore` excludes generated keys, build output, and downloaded data.

If real AWS keys were ever committed or shared, rotate/delete those keys in IAM immediately.

## Expected Layout

Default paths expect this layout:

```text
repo-root/
  binance_klines_collector_cpp20.cpp
  python AWS test/
    deploy_binance_collector_to_ec2.py
    get_instance_logs.py
    decode_kline_bin.py
    launch_with_ssh.py
    requirements.txt
```

If your C++ file is elsewhere, edit `CONFIG_SOURCE` in `deploy_binance_collector_to_ec2.py`.

## Requirements

Windows with PowerShell, Python 3.10+, WSL Ubuntu, and AWS credentials.

Install Python dependencies:

```powershell
cd "C:\C++ learning\python AWS test"
python -m pip install -r requirements.txt
```

Install WSL build dependencies once:

```powershell
wsl.exe bash -lc "sudo apt update && sudo apt install -y build-essential pkg-config libcurl4-openssl-dev libssl-dev libboost-dev libboost-system-dev nlohmann-json3-dev libpugixml-dev libzip-dev"
```

## AWS Credentials

Recommended: configure credentials outside the repo.

```powershell
aws configure
```

Or set environment variables:

```powershell
$env:AWS_ACCESS_KEY_ID="..."
$env:AWS_SECRET_ACCESS_KEY="..."
$env:AWS_DEFAULT_REGION="eu-west-2"
```

There are optional `HARDCODED_AWS_...` constants in `deploy_binance_collector_to_ec2.py` for local private experiments. Leave them blank for GitHub.

## Quick Start

Edit the configuration block near the top of `deploy_binance_collector_to_ec2.py`, then run:

```powershell
cd "C:\C++ learning\python AWS test"
python .\deploy_binance_collector_to_ec2.py
```

The default flow:

1. Compiles `binance_klines_collector_cpp20.cpp` into a Linux ELF binary using WSL.
2. Uploads the binary to a private S3 bucket using a temporary presigned URL.
3. Creates/reuses an EC2 SSH key pair.
4. Creates/reuses a security group with SSH from your current public IP.
5. Launches Ubuntu 22.04 on EC2.
6. Installs runtime packages.
7. Installs and starts `binance-collector.service`.
8. Prints setup logs until `BOOTSTRAP_DONE`.
9. Prints the latest `.bin` kline row every `CONFIG_DATA_POLL_SECONDS`.

Press `Ctrl+C` to stop watching logs/data. That does not terminate the instance.

## Main Config

All normal inputs are editable as variables in `deploy_binance_collector_to_ec2.py`.

| Variable | Meaning |
|---|---|
| `CONFIG_MODE` | `"wsl-binary"` builds locally and uploads a binary. `"ec2-source"` uploads C++ source and compiles on EC2. |
| `CONFIG_SOURCE` | Path to `binance_klines_collector_cpp20.cpp`. |
| `CONFIG_BUILD_DIR` | Local folder for the Linux binary. |
| `CONFIG_BINARY_NAME` | Name of the Linux executable on disk and on EC2. |
| `CONFIG_SERVICE_NAME` | `systemd` service name and EC2 Name tag. |
| `CONFIG_PROGRAM_ARGS` | List of command-line args passed to the C++ program. Usually empty because the C++ config is compiled in. |
| `CONFIG_REGION` | AWS region, default `eu-west-2`. |
| `CONFIG_AMI_ID` | Optional Ubuntu AMI ID override. Empty uses latest Ubuntu 22.04 from SSM. |
| `CONFIG_INSTANCE_TYPE` | EC2 size, default `t2.nano`. |
| `CONFIG_VOLUME_GB` | Root EBS volume size in GB. |
| `CONFIG_SUBNET_ID` | Optional subnet ID. Empty uses a default subnet. |
| `CONFIG_SECURITY_GROUP_ID` | Optional existing security group ID. |
| `CONFIG_SSH_SECURITY_GROUP_ID` | Optional existing SSH/inbound security group ID. |
| `CONFIG_INBOUND_SECURITY_GROUP_ID` | Alias for passing an existing inbound security group. |
| `CONFIG_ENABLE_SSH` | If `True`, create/use SSH key and SSH group. |
| `CONFIG_KEY_NAME` | EC2 key pair name. |
| `CONFIG_KEY_PATH` | Local private key path. Default under `keys/`. |
| `CONFIG_SSH_CIDR` | CIDR allowed for SSH. Empty auto-detects current public IP and uses `/32`. |
| `CONFIG_AUTHORIZE_SSH` | If `True`, add port 22 ingress to the chosen SSH group. |
| `CONFIG_REPLACE_SSH_KEY` | If `True`, delete/recreate the AWS key pair. Use carefully. |
| `CONFIG_OPEN_SSH_AFTER_LAUNCH` | Opens an SSH terminal after launch. |
| `CONFIG_SSH_SAME_WINDOW` | Runs SSH in the current terminal instead of a new window. |
| `CONFIG_SSH_TIMEOUT_SECONDS` | How long to wait for port 22. |
| `CONFIG_SSH_USER` | SSH username, `ubuntu` for Ubuntu AMIs. |
| `CONFIG_FETCH_LOGS_AFTER_LAUNCH` | Fetch logs after launch. |
| `CONFIG_LOG_TARGET` | `"bootstrap"`, `"service"`, or `"both"`. |
| `CONFIG_LOG_LINES` | Number of recent log lines to print first. |
| `CONFIG_FOLLOW_LOGS` | If `True`, follow logs live. |
| `CONFIG_BOOTSTRAP_LOG_TIMEOUT_SECONDS` | Max time to wait for `BOOTSTRAP_DONE`. |
| `CONFIG_PRINT_LAST_BIN_ROW_AFTER_LOGS` | After setup logs, print latest `.bin` row repeatedly. |
| `CONFIG_REMOTE_DATA_DIR` | Data folder on EC2. Default `/opt/binance-collector/data_dump`. |
| `CONFIG_DATA_POLL_SECONDS` | Delay between latest `.bin` row prints. |
| `CONFIG_IAM_INSTANCE_PROFILE` | Optional IAM instance profile name/ARN. Not required for the presigned URL flow. |
| `CONFIG_BUCKET` | Optional S3 bucket. Empty creates/uses `ec2-linux-program-deploy-ACCOUNT-REGION`. |
| `CONFIG_PRESIGN_HOURS` | Temporary S3 URL lifetime. |
| `CONFIG_COMPILE_ONLY` | If `True`, compile locally and stop. |
| `CONFIG_SKIP_BUILD` | If `True`, upload an existing binary from `CONFIG_BINARY_PATH`. |
| `CONFIG_BINARY_PATH` | Existing Linux binary path when skipping build. |
| `CONFIG_CONFIRM_AWS_ACTIONS` | Must be `True` for launch/terminate when running without `--yes`. |
| `CONFIG_TERMINATE_INSTANCE_ID` | Instance ID to terminate, then exit. |
| `CONFIG_NO_WAIT` | If `True`, do not wait for EC2 running state. |

## Common Workflows

Compile only:

```powershell
python .\deploy_binance_collector_to_ec2.py --compile-only
```

Launch with defaults:

```powershell
python .\deploy_binance_collector_to_ec2.py
```

Launch and override the instance type:

```powershell
python .\deploy_binance_collector_to_ec2.py --instance-type t3.medium
```

Use an existing SSH group:

```powershell
python .\deploy_binance_collector_to_ec2.py --ssh-security-group-id sg-xxxxxxxxxxxxxxxxx
```

Compile on EC2 instead of locally:

```powershell
python .\deploy_binance_collector_to_ec2.py --mode ec2-source --instance-type t3.medium
```

Terminate an instance:

```powershell
python .\deploy_binance_collector_to_ec2.py --terminate-instance i-xxxxxxxxxxxxxxxxx --yes
```

## Optional CLI Inputs

Every config variable has a command-line override. Use `--help` for the exact list:

```powershell
python .\deploy_binance_collector_to_ec2.py --help
```

Deploy CLI flags:

| Flag | Meaning |
|---|---|
| `--mode {wsl-binary,ec2-source}` | Local binary build or EC2 source build. |
| `--source PATH` | C++ source path. |
| `--build-dir PATH` | Local build output directory. |
| `--binary-name NAME` | Executable name. |
| `--service-name NAME` | Service/tag name. |
| `--program-arg VALUE` | Add an argument to the service command. Repeatable. |
| `--region REGION` | AWS region. |
| `--ami-id AMI` | Ubuntu AMI override. |
| `--instance-type TYPE` | EC2 instance type. |
| `--volume-gb N` | Root volume size. |
| `--subnet-id subnet-...` | Existing subnet. |
| `--security-group-id sg-...` | Existing group. |
| `--ssh-security-group-id sg-...` | Existing SSH/inbound group. |
| `--inbound-security-group-id sg-...` | Alias for an existing inbound group. |
| `--no-ssh` | Disable automatic SSH setup. |
| `--key-name NAME` | EC2 key pair name. |
| `--key-path PATH` | Local private key path. |
| `--ssh-cidr CIDR` | SSH source, for example `1.2.3.4/32`. |
| `--no-authorize-ssh` | Do not add SSH ingress. |
| `--replace-ssh-key` | Recreate the AWS key pair. |
| `--no-open-ssh` | Do not open an SSH terminal. |
| `--ssh-same-window` | SSH in current terminal. |
| `--ssh-timeout N` | Seconds to wait for SSH. |
| `--ssh-user USER` | SSH user, usually `ubuntu`. |
| `--no-fetch-logs` | Skip log fetch after launch. |
| `--log-target bootstrap/service/both` | Which logs to show. |
| `--log-lines N` | Number of recent lines to print before following. |
| `--no-follow-logs` | Print logs once instead of following. |
| `--bootstrap-log-timeout N` | Seconds to wait for setup completion. |
| `--no-print-bin-row` | Do not watch latest `.bin` row. |
| `--remote-data-dir PATH` | Remote data folder to watch. |
| `--data-poll-seconds N` | Latest row polling delay. |
| `--iam-instance-profile NAME_OR_ARN` | Optional EC2 IAM instance profile. |
| `--bucket BUCKET` | S3 upload bucket. |
| `--presign-hours N` | Presigned S3 URL lifetime. |
| `--compile-only` | Build Linux binary and stop. |
| `--skip-build --binary-path PATH` | Upload existing Linux binary. |
| `--yes` | Confirm paid AWS launch/terminate action. |
| `--terminate-instance i-...` | Terminate instance and exit. |
| `--no-wait` | Launch and exit without waiting for EC2 running state. |

## Logs and Data

The collector runs on EC2 under:

```text
/opt/binance-collector
```

Runtime output:

```text
/var/log/binance-collector-bootstrap.log
journalctl -u binance-collector
/opt/binance-collector/data_dump
```

Fetch logs from an existing instance:

```powershell
python .\get_instance_logs.py --instance-id i-xxxxxxxxxxxxxxxxx --service --follow
python .\get_instance_logs.py --instance-id i-xxxxxxxxxxxxxxxxx --bootstrap --lines 200
python .\get_instance_logs.py --instance-id i-xxxxxxxxxxxxxxxxx --console
```

List saved `.bin`, `.csv`, and `.log` files:

```powershell
python .\get_instance_logs.py --instance-id i-xxxxxxxxxxxxxxxxx --list-data
```

Download and extract the whole `data_dump` folder:

```powershell
python .\get_instance_logs.py --instance-id i-xxxxxxxxxxxxxxxxx --download-data
```

`get_instance_logs.py` config inputs:

| Variable | Meaning |
|---|---|
| `CONFIG_INSTANCE_ID` | Instance to inspect. Empty means newest `Name=binance-collector`. |
| `CONFIG_NAME` | EC2 Name tag used for auto-discovery. |
| `CONFIG_REGION` | AWS region. |
| `CONFIG_LINES` | Number of log lines. |
| `CONFIG_RAW` | Disable redaction. |
| `CONFIG_CONSOLE` | Fetch EC2 console log. |
| `CONFIG_SERVICE` | Fetch service journal. |
| `CONFIG_BOOTSTRAP` | Fetch bootstrap log. |
| `CONFIG_CLOUD_INIT` | Fetch cloud-init log. |
| `CONFIG_FOLLOW` | Follow logs live. |
| `CONFIG_KEY_PATH` | SSH private key path. |
| `CONFIG_USER` | SSH user. |
| `CONFIG_LIST_DATA` | List data files. |
| `CONFIG_DOWNLOAD_DATA` | Download data archive. |
| `CONFIG_REMOTE_DATA_DIR` | Remote data folder. |
| `CONFIG_LOCAL_DOWNLOAD_DIR` | Local download folder. |
| `CONFIG_EXTRACT_DOWNLOAD` | Extract `.tgz` after download. |

`get_instance_logs.py` CLI flags:

| Flag | Meaning |
|---|---|
| `--instance-id i-...` | Instance to inspect. |
| `--name NAME` | EC2 Name tag to auto-discover when no instance ID is given. |
| `--region REGION` | AWS region. |
| `--lines N` | Number of log lines. |
| `--raw` | Disable redaction. |
| `--console` | Print EC2 console output. |
| `--service` | Print `journalctl -u binance-collector`. |
| `--bootstrap` | Print `/var/log/binance-collector-bootstrap.log`. |
| `--cloud-init` | Print cloud-init output. |
| `--follow` | Follow logs live. |
| `--key-path PATH` | SSH private key. |
| `--user USER` | SSH user. |
| `--list-data` | List saved `.bin`, `.csv`, and `.log` files. |
| `--download-data` | Download `data_dump` as `.tgz`. |
| `--remote-data-dir PATH` | Remote data folder. |
| `--local-download-dir PATH` | Local download folder. |
| `--no-extract` | Keep archive only, do not extract. |

## Decode `.bin` Files

The C++ collector writes packed 128-byte kline records. Decode a downloaded file:

```powershell
python .\decode_kline_bin.py "downloaded_data\...\BTCUSDT_spot_1m_2026-06-04.bin"
```

Show more rows:

```powershell
python .\decode_kline_bin.py "path\to\file.bin" --limit 100
```

Print CSV:

```powershell
python .\decode_kline_bin.py "path\to\file.bin" --csv > rows.csv
```

`decode_kline_bin.py` inputs:

| Input | Meaning |
|---|---|
| `bin_file` | Path to a downloaded collector `.bin` file. |
| `--limit N` | Maximum rows to print. |
| `--csv` | Write CSV to stdout instead of human-readable lines. |

Decoded fields:

```text
symbol, open_time_ms, close_time_ms, open, high, low, close, volume,
quote_volume, trades, taker_base_vol, taker_quote_vol,
maker_base_vol, maker_quote_vol
```

## SSH Helper

`launch_with_ssh.py` is a smaller wrapper around the same SSH launch idea. The main deploy script already starts with SSH by default, so most users do not need this file.

Run it:

```powershell
python .\launch_with_ssh.py
```

Useful variables:

| Variable | Meaning |
|---|---|
| `CONFIG_INSTANCE_TYPE` | EC2 instance type. |
| `CONFIG_SECURITY_GROUP_ID` | Existing security group. |
| `CONFIG_SSH_SECURITY_GROUP_ID` | Existing SSH group. |
| `CONFIG_SSH_CIDR` | SSH source CIDR. |
| `CONFIG_NO_AUTHORIZE_SSH` | Use group without changing ingress. |
| `CONFIG_KEY_NAME` | EC2 key pair name. |
| `CONFIG_KEY_PATH` | Local private key path. |
| `CONFIG_BINARY_PATH` | Existing binary to upload. |
| `CONFIG_REBUILD` | Force rebuild. |
| `CONFIG_NO_CONNECT` | Launch only. |

`launch_with_ssh.py` CLI flags:

| Flag | Meaning |
|---|---|
| `--region REGION` | AWS region. |
| `--instance-type TYPE` | EC2 size. |
| `--volume-gb N` | Root volume size. |
| `--subnet-id subnet-...` | Existing subnet. |
| `--security-group-id sg-...` | Existing security group. |
| `--ssh-security-group-id sg-...` | Existing SSH group. |
| `--ssh-cidr CIDR` | SSH source CIDR. |
| `--no-authorize-ssh` | Do not edit security-group ingress. |
| `--key-name NAME` | EC2 key pair name. |
| `--key-path PATH` | Local private key path. |
| `--binary-path PATH` | Existing binary path. |
| `--rebuild` | Force rebuild before launch. |
| `--replace-key` | Delete/recreate the AWS key pair. |
| `--no-connect` | Do not open SSH after launch. |
| `--same-window` | SSH in the current terminal. |
| `--ssh-timeout N` | Seconds to wait for SSH. |
| `--user USER` | SSH user. |

## Cost Notes

`t2.nano` is cheap but tiny. It is suitable for running a small already-built binary, not compiling heavy C++ source on EC2.

For `--mode ec2-source`, use a larger instance such as `t3.medium`, then terminate it when done.

Set an AWS Budget in the AWS Billing console. AWS Budgets are alerts/actions, not a perfect hard spending cap.

## Troubleshooting

`Refusing to launch a paid EC2 instance without --yes`: set `CONFIG_CONFIRM_AWS_ACTIONS = True` or pass `--yes`.

`Missing Python AWS dependency`: run `python -m pip install -r requirements.txt`.

`WSL is missing build dependencies`: run the WSL install command in the Requirements section.

`Identity file C:\C++ not accessible` or `Could not resolve hostname learning\python`: the SSH key path was split by spaces. Current scripts quote paths correctly. Use the printed PowerShell command beginning with `& 'ssh' ...`.

`SSH timed out`: your public IP may have changed. Relaunch, or update the security group rule for your current IP.

`Existing instances launched without a key`: they usually cannot be SSH'd into. Relaunch with SSH enabled.

`No .bin files found yet`: the service may still be starting, no symbol has closed a candle yet, or the configured `BASE_DIR` changed.
