# EC2 Instance Manager

`manage_ec2_instance.py` starts, stops, reboots, hibernates, terminates, or shows status for existing EC2 instances.

It is intentionally separate from the collector deploy script. Use it when you already have an instance ID and want to control that instance.

## Setup

Install Python dependencies:

```powershell
cd "C:\C++ learning\python AWS test"
python -m pip install -r requirements.txt
```

Configure AWS credentials outside the repository:

```powershell
aws configure
```

Or set environment variables:

```powershell
$env:AWS_ACCESS_KEY_ID="..."
$env:AWS_SECRET_ACCESS_KEY="..."
$env:AWS_DEFAULT_REGION="eu-west-2"
```

The script also has optional `HARDCODED_AWS_...` fields for local private use. Leave them blank for GitHub.

## Quick Start With Variables

Edit the top of `manage_ec2_instance.py`:

```python
CONFIG_ACTION = "status"
CONFIG_INSTANCE_IDS = ["i-xxxxxxxxxxxxxxxxx"]
CONFIG_REGION = "eu-west-2"
CONFIG_CONFIRM_ACTIONS = False
```

Then run:

```powershell
python .\manage_ec2_instance.py
```

For actions that change the instance, set:

```python
CONFIG_CONFIRM_ACTIONS = True
```

## Actions

| Action | Meaning | Requires confirmation |
|---|---|---|
| `status` | Print instance state, type, Name tag, IP, and launch time. | No |
| `start` | Start a stopped instance. | Yes |
| `stop` | Stop a running instance. EBS volumes remain. | Yes |
| `reboot` | Reboot a running instance. | Yes |
| `hibernate` | Hibernate an instance if it was launched with hibernation support. | Yes |
| `terminate` | Permanently delete the instance. Usually deletes root EBS volume. | Yes |

## Config Inputs

| Variable | Meaning |
|---|---|
| `CONFIG_ACTION` | One of `status`, `start`, `stop`, `reboot`, `hibernate`, `terminate`. |
| `CONFIG_INSTANCE_IDS` | List of EC2 instance IDs to manage. |
| `CONFIG_INSTANCE_NAME` | Optional exact EC2 `Name` tag lookup if no IDs are provided. |
| `CONFIG_REGION` | AWS region, default from `AWS_DEFAULT_REGION` or `eu-west-2`. |
| `CONFIG_CONFIRM_ACTIONS` | Must be `True` for changing actions. |
| `CONFIG_WAIT` | Wait for final state after `start`, `stop`, `hibernate`, or `terminate`. |
| `CONFIG_FORCE_STOP` | Force stop for `stop`. Use only if normal stop is stuck. |
| `CONFIG_DRY_RUN` | Ask AWS to validate permissions without doing the action. |
| `CONFIG_SHOW_PUBLIC_IP` | Include public IP column in status output. |
| `HARDCODED_AWS_ACCESS_KEY_ID` | Optional local hardcoded access key. Do not commit. |
| `HARDCODED_AWS_SECRET_ACCESS_KEY` | Optional local hardcoded secret key. Do not commit. |
| `HARDCODED_AWS_SESSION_TOKEN` | Optional STS/session token. |
| `HARDCODED_AWS_REGION` | Optional region override for hardcoded credential sessions. |

## Command-Line Usage

You can use command-line inputs instead of editing variables.

Status:

```powershell
python .\manage_ec2_instance.py --action status --instance-id i-xxxxxxxxxxxxxxxxx
```

Start:

```powershell
python .\manage_ec2_instance.py --action start --instance-id i-xxxxxxxxxxxxxxxxx --yes
```

Stop:

```powershell
python .\manage_ec2_instance.py --action stop --instance-id i-xxxxxxxxxxxxxxxxx --yes
```

Reboot:

```powershell
python .\manage_ec2_instance.py --action reboot --instance-id i-xxxxxxxxxxxxxxxxx --yes
```

Hibernate:

```powershell
python .\manage_ec2_instance.py --action hibernate --instance-id i-xxxxxxxxxxxxxxxxx --yes
```

Terminate:

```powershell
python .\manage_ec2_instance.py --action terminate --instance-id i-xxxxxxxxxxxxxxxxx --yes
```

Manage multiple instances:

```powershell
python .\manage_ec2_instance.py --action status --instance-id i-aaa --instance-id i-bbb
```

Find by exact `Name` tag:

```powershell
python .\manage_ec2_instance.py --action status --name binance-collector
```

## CLI Inputs

| Flag | Meaning |
|---|---|
| `--action ACTION` | `status`, `start`, `stop`, `reboot`, `hibernate`, or `terminate`. |
| `--instance-id i-...` | EC2 instance ID. Repeat for multiple instances. |
| `--name NAME` | Find instances by exact Name tag when no instance ID is supplied. |
| `--region REGION` | AWS region. |
| `--yes` | Confirm actions that change state. |
| `--no-wait` | Send request and exit without waiting. |
| `--force-stop` | Force stop the instance. |
| `--dry-run` | Validate AWS permissions without making the change. |
| `--no-public-ip` | Hide public IP in status output. |

## Hibernation Notes

`hibernate` only works when the instance was launched with hibernation enabled and the instance type/AMI/root volume support it.

If hibernation is not enabled, AWS will return an error. Use `stop` instead, or relaunch the instance with hibernation configured.

## Cost Notes

Stopped instances usually stop compute charges, but EBS volumes and snapshots can still cost money.

Terminated instances are deleted. If the root volume has `DeleteOnTermination=True`, the root disk is deleted too.

Use AWS Budgets for alerts, and always verify state in the EC2 console if cost matters.

## Troubleshooting

`No instances supplied`: set `CONFIG_INSTANCE_IDS`, pass `--instance-id`, or set `CONFIG_INSTANCE_NAME`.

`Refusing to stop/start/reboot/hibernate/terminate`: set `CONFIG_CONFIRM_ACTIONS = True` or pass `--yes`.

`Missing boto3`: run `python -m pip install -r requirements.txt`.

`DryRunOperation`: your permissions are valid; rerun without `--dry-run`.

`UnauthorizedOperation`: your AWS user/role does not have the required EC2 permission.

`IncorrectInstanceState`: the requested action does not apply to the current instance state, for example starting an already running instance.
