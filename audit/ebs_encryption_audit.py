#!/usr/bin/env python3
"""
EBS Encryption Audit - all accounts (prod / qa / uat), all regions

Paste the export block from AWS Identity Center when prompted, once per
account.

Multi-account and multi-region: every enabled region is checked for each
account, so volumes running somewhere unexpected still show up in the
output.

Output: a single combined JSON file (ebs_encryption_<timestamp>.json)
containing a flat list of volume records. Each record carries its own
"account" and "region" fields.
"""

import json
import sys
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

ACCOUNTS = ["prod", "qa", "uat"]
CFN_TAG_KEY = "aws:cloudformation:stack-name"


def get_name_tag(tags):
    if not tags:
        return None
    for tag in tags:
        if tag.get("Key") == "Name":
            return tag.get("Value")
    return None


def get_customer_tag(tags):
    if not tags:
        return None
    for tag in tags:
        if tag.get("Key") == "Customer":
            return tag.get("Value")
    return None


def get_cfn_stack_tag(tags):
    if not tags:
        return None
    for tag in tags:
        if tag.get("Key") == CFN_TAG_KEY:
            return tag.get("Value")
    return None


def paste_aws_exports(account_label):
    print("\n" + "─" * 50)
    print("  Credentials for " + account_label.upper())
    print("─" * 50)
    print("Paste the AWS export block below.")
    print("Press ENTER on a blank line when done.\n")

    lines = []
    while True:
        line = input()
        if line.strip() == "":
            break
        lines.append(line)

    text = "\n".join(lines)
    creds = {}
    import re
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        m = re.search(key + r'\s*=\s*["\']?([^"\']+)["\']?', text)
        if m:
            creds[key] = m.group(1).strip()

    missing = [k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
               if k not in creds]
    if missing:
        print("\nMissing credentials:")
        for k in missing:
            print("  - " + k)
        sys.exit(1)

    return creds


def validate_credentials(session, account_label):
    try:
        identity = session.client("sts").get_caller_identity()
        print(f"  Authenticated — Account: {identity.get('Account')}")
        return True
    except (ClientError, NoCredentialsError) as e:
        print(f"  Authentication failed for {account_label.upper()}: {e}")
        return False


def get_enabled_regions(session):
    ec2 = session.client("ec2", region_name="us-east-1")
    try:
        r = ec2.describe_regions(Filters=[{"Name": "opt-in-status",
                                            "Values": ["opt-in-not-required", "opted-in"]}])
        return sorted([x["RegionName"] for x in r["Regions"]])
    except ClientError as e:
        print(f"  Failed to retrieve regions: {e}")
        sys.exit(1)


def audit_region(session, region, account):
    ec2 = session.client("ec2", region_name=region)
    results = []

    # Build instance_id -> {state, customer, cfn_stack_name} map
    instance_map = {}
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                state = instance["State"]["Name"]
                customer = get_customer_tag(instance.get("Tags", []))
                cfn_stack_name = get_cfn_stack_tag(instance.get("Tags", []))
                instance_map[instance_id] = {
                    "state": state,
                    "customer": customer,
                    "cfn_stack_name": cfn_stack_name,
                }

    # Walk volumes
    vol_paginator = ec2.get_paginator("describe_volumes")
    for page in vol_paginator.paginate():
        for volume in page.get("Volumes", []):
            volume_id = volume["VolumeId"]
            name = get_name_tag(volume.get("Tags", [])) or "None"
            encrypted = volume.get("Encrypted", False)
            size = volume.get("Size")

            attachments = volume.get("Attachments", [])
            instance_id = attachments[0]["InstanceId"] if attachments else None

            # CFN stack-name tag: prefer the tag directly on the volume;
            # fall back to the attached instance's tag if the volume has none.
            volume_cfn_stack_name = get_cfn_stack_tag(volume.get("Tags", []))

            if instance_id and instance_id in instance_map:
                ec2_state = instance_map[instance_id]["state"]
                customer = instance_map[instance_id]["customer"] or "untagged"
                instance_cfn_stack_name = instance_map[instance_id]["cfn_stack_name"]
            elif instance_id:
                ec2_state = "unknown"
                customer = "unknown"
                instance_cfn_stack_name = None
            else:
                ec2_state = "unattached"
                customer = "n/a"
                instance_cfn_stack_name = None

            cfn_stack_name = volume_cfn_stack_name or instance_cfn_stack_name
            confirmed = cfn_stack_name is not None

            results.append({
                "account": account,
                "name": name,
                "volume_id": volume_id,
                "region": region,
                "encrypted": encrypted,
                "size_gib": size,
                "ec2_instance_id": instance_id or "None",
                "ec2_instance_state": ec2_state,
                "customer": customer,
                "cfn_stack_name": cfn_stack_name or "None",
                "confirmed": confirmed,
            })

    return results


def audit_account(account, creds):
    print(f"\n  {account.upper()}")
    session = boto3.Session(
        aws_access_key_id=creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=creds["AWS_SESSION_TOKEN"],
        region_name="us-east-1",
    )
    if not validate_credentials(session, account):
        return []

    account_results = []
    for region in get_enabled_regions(session):
        print(f"    Scanning {account} / {region} ...")
        try:
            region_results = audit_region(session, region, account)
            account_results.extend(region_results)
            confirmed_count = sum(1 for r in region_results if r["confirmed"])
            print(f"      found {len(region_results)} volumes "
                  f"({confirmed_count} confirmed, "
                  f"{len(region_results) - confirmed_count} unconfirmed)")
        except NoCredentialsError:
            print("      Credentials not found or invalid. Aborting.")
            sys.exit(1)
        except ClientError as e:
            print(f"      ERROR in {region}: {e}")

    return account_results


def main():
    print("\nEBS Encryption Audit — all accounts, all regions")
    print("--------------------------------------------------")
    print(f"This audit covers: {', '.join(a.upper() for a in ACCOUNTS)}")

    all_creds = {}
    for account in ACCOUNTS:
        all_creds[account] = paste_aws_exports(account)

    print("\nAll credentials collected. Starting audit...")

    all_results = []
    for account in ACCOUNTS:
        all_results.extend(audit_account(account, all_creds[account]))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"ebs_encryption_{ts}.json"
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2)

    total_confirmed = sum(1 for r in all_results if r["confirmed"])
    print(f"\nDone. {len(all_results)} total volumes written to {output_file}")
    print(f"  Confirmed (has CFN stack-name tag): {total_confirmed}")
    print(f"  Unconfirmed (no CFN stack-name tag): {len(all_results) - total_confirmed}")


if __name__ == "__main__":
    main()
