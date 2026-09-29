import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

CFN_TAG_KEY = "aws:cloudformation:stack-name"
KMS_KEY_ID = "alias/aws/ebs"
SOURCE_VOLUME_TAG = "SourceVolumeId"


def get_tag_value(tags, key_name):
    if not tags:
        return None
    for tag in tags:
        if tag.get("Key") == key_name:
            return tag.get("Value")
    return None


def get_cfn_stack_tag(tags):
    return get_tag_value(tags, CFN_TAG_KEY)


def get_name_tag(tags):
    return get_tag_value(tags, "Name")


def strip_reserved_tags(tags):
    # AWS rejects any tag key starting with "aws:" on CreateVolume -
    # those are reserved for AWS-managed tags (e.g. the CFN stack tag)
    # and get reapplied by AWS automatically regardless.
    return [t for t in (tags or []) if not t.get("Key", "").startswith("aws:")]


def find_snapshot_by_name(ec2, name):
    resp = ec2.describe_snapshots(
        OwnerIds=["self"],
        Filters=[{"Name": "tag:Name", "Values": [name]}],
    )
    snaps = resp.get("Snapshots", [])
    return snaps[0] if snaps else None


def find_volume_by_tag(ec2, tag_key, tag_value):
    resp = ec2.describe_volumes(
        Filters=[{"Name": f"tag:{tag_key}", "Values": [tag_value]}],
    )
    vols = resp.get("Volumes", [])
    return vols[0] if vols else None


def get_or_create_unencrypted_snapshot(ec2, volume_id, name, log_prefix):
    existing = find_snapshot_by_name(ec2, name)
    if existing:
        snapshot_id = existing["SnapshotId"]
        print(f"{log_prefix} Snapshot {name} already exists ({snapshot_id}), skipping creation...")
    else:
        print(f"{log_prefix} Creating snapshot {name}...")
        resp = ec2.create_snapshot(
            VolumeId=volume_id,
            Description=name,
            TagSpecifications=[{
                "ResourceType": "snapshot",
                "Tags": [{"Key": "Name", "Value": name}],
            }],
        )
        snapshot_id = resp["SnapshotId"]

    print(f"{log_prefix} Waiting for {snapshot_id} to complete...")
    ec2.get_waiter("snapshot_completed").wait(
        SnapshotIds=[snapshot_id],
        WaiterConfig={"Delay": 15, "MaxAttempts": 180},
    )
    return snapshot_id


def get_or_create_encrypted_snapshot(ec2, source_snapshot_id, name, log_prefix):
    existing = find_snapshot_by_name(ec2, name)
    if existing:
        snapshot_id = existing["SnapshotId"]
        print(f"{log_prefix} Encrypted snapshot {name} already exists ({snapshot_id}), skipping copy...")
    else:
        print(f"{log_prefix} Copying to encrypted snapshot {name}...")
        resp = ec2.copy_snapshot(
            SourceSnapshotId=source_snapshot_id,
            SourceRegion=ec2.meta.region_name,
            Encrypted=True,
            KmsKeyId=KMS_KEY_ID,
            Description=name,
            TagSpecifications=[{
                "ResourceType": "snapshot",
                "Tags": [{"Key": "Name", "Value": name}],
            }],
        )
        snapshot_id = resp["SnapshotId"]

    print(f"{log_prefix} Waiting for {snapshot_id} to be available...")
    ec2.get_waiter("snapshot_completed").wait(
        SnapshotIds=[snapshot_id],
        WaiterConfig={"Delay": 15, "MaxAttempts": 180},
    )
    return snapshot_id


def get_or_create_tagged_volume(ec2, snapshot_id, az, volume_type, tags, lookup_tag_key, lookup_tag_value, log_prefix):
    existing = find_volume_by_tag(ec2, lookup_tag_key, lookup_tag_value)
    if existing:
        volume_id = existing["VolumeId"]
        print(f"{log_prefix} Volume already exists ({volume_id}) for {lookup_tag_key}={lookup_tag_value}, skipping creation...")
    else:
        print(f"{log_prefix} Creating new volume from snapshot {snapshot_id}, tagged to match original...")
        tag_list = strip_reserved_tags(tags)
        tag_list = [t for t in tag_list if t.get("Key") != lookup_tag_key]
        tag_list.append({"Key": lookup_tag_key, "Value": lookup_tag_value})
        resp = ec2.create_volume(
            SnapshotId=snapshot_id,
            AvailabilityZone=az,
            VolumeType=volume_type,
            TagSpecifications=[{"ResourceType": "volume", "Tags": tag_list}],
        )
        volume_id = resp["VolumeId"]

    print(f"{log_prefix} Waiting for {volume_id} to become available...")
    ec2.get_waiter("volume_available").wait(
        VolumeIds=[volume_id],
        WaiterConfig={"Delay": 15, "MaxAttempts": 60},
    )
    return volume_id


def wait_for_volume_state(ec2, volume_id, target_state, log_prefix):
    print(f"{log_prefix} Waiting for {volume_id} to reach state '{target_state}'...")
    while True:
        resp = ec2.describe_volumes(VolumeIds=[volume_id])
        state = resp["Volumes"][0]["State"]
        if state == target_state:
            print(f"{log_prefix} {volume_id} is now '{target_state}'.")
            return
        time.sleep(10)


def process_instance(instance_id, instance_name, volumes):
    log_prefix = f"[{instance_name}]"
    session = boto3.Session()
    ec2 = session.client("ec2")

    try:
        volumes_needing_work = [v for v in volumes if not v["encrypted"]]

        if not volumes_needing_work:
            print(f"{log_prefix} All volumes already encrypted. No action needed.")
            return {
                "instance_name": instance_name,
                "instance_id": instance_id,
                "status": "skipped_already_encrypted",
                "volumes_swapped": 0,
                "public_ip": None,
            }

        swap_plan = []
        for v in volumes_needing_work:
            volume_id = v["volume_id"]
            device = v["device"]
            print(f"{log_prefix} Preparing {volume_id} ({device})...")

            desc = ec2.describe_volumes(VolumeIds=[volume_id])
            volume = desc["Volumes"][0]
            az = volume["AvailabilityZone"]
            volume_type = volume.get("VolumeType", "gp3")
            tags = volume.get("Tags", [])

            unenc_name = f"{instance_name}-{volume_id}-final-unencrypted-snap"
            enc_name = f"{instance_name}-{volume_id}-encrypted-snap"

            unenc_snapshot_id = get_or_create_unencrypted_snapshot(ec2, volume_id, unenc_name, log_prefix)
            enc_snapshot_id = get_or_create_encrypted_snapshot(ec2, unenc_snapshot_id, enc_name, log_prefix)
            new_volume_id = get_or_create_tagged_volume(
                ec2, enc_snapshot_id, az, volume_type, tags,
                SOURCE_VOLUME_TAG, volume_id, log_prefix,
            )
            swap_plan.append((volume_id, new_volume_id, device))

        print(f"{log_prefix} Stopping instance {instance_id}...")
        ec2.stop_instances(InstanceIds=[instance_id])
        ec2.get_waiter("instance_stopped").wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 15, "MaxAttempts": 60},
        )
        print(f"{log_prefix} Instance stopped.")

        for old_volume_id, new_volume_id, device in swap_plan:
            print(f"{log_prefix} Detaching {old_volume_id} from {device}...")
            ec2.detach_volume(VolumeId=old_volume_id, InstanceId=instance_id, Device=device)
            wait_for_volume_state(ec2, old_volume_id, "available", log_prefix)

            print(f"{log_prefix} Attaching {new_volume_id} to {device}...")
            ec2.attach_volume(VolumeId=new_volume_id, InstanceId=instance_id, Device=device)
            wait_for_volume_state(ec2, new_volume_id, "in-use", log_prefix)

        print(f"{log_prefix} Starting instance {instance_id}...")
        ec2.start_instances(InstanceIds=[instance_id])

        print(f"{log_prefix} Waiting for instance to pass all status checks (this can take several minutes)...")
        ec2.get_waiter("instance_status_ok").wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 15, "MaxAttempts": 60},
        )
        print(f"{log_prefix} Instance running, all status checks passed.")

        resp = ec2.describe_instances(InstanceIds=[instance_id])
        info = resp["Reservations"][0]["Instances"][0]
        public_ip = info.get("PublicIpAddress", "None (no public IP)")

        return {
            "instance_name": instance_name,
            "instance_id": instance_id,
            "status": "encrypted",
            "volumes_swapped": len(swap_plan),
            "public_ip": public_ip,
        }

    except Exception as e:
        print(f"{log_prefix} ERROR: {e}")
        return {
            "instance_name": instance_name,
            "instance_id": instance_id,
            "status": "failed",
            "error": str(e),
            "volumes_swapped": 0,
            "public_ip": None,
        }


def validate_stack(environment, env_instance):
    stack_id = f"{environment}-{env_instance}"
    print(f"--- Validating stack: {stack_id} ---")

    ec2 = boto3.client("ec2")

    instances = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[{"Name": f"tag:{CFN_TAG_KEY}", "Values": [stack_id]}]
    ):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instances.append(instance)

    if not instances:
        print(f"ERROR: No EC2 instances found with {CFN_TAG_KEY}={stack_id}.")
        print("This stack cannot be validated. Aborting.")
        sys.exit(1)

    print(f"Found {len(instances)} instance(s) tagged with stack '{stack_id}':")
    for instance in instances:
        name = get_name_tag(instance.get("Tags", [])) or "(no Name tag)"
        print(f"  {instance['InstanceId']}  {name}  state={instance['State']['Name']}")

    all_confirmed = True
    stack_volumes = []

    for instance in instances:
        instance_id = instance["InstanceId"]
        instance_cfn_tag = get_cfn_stack_tag(instance.get("Tags", []))

        for block_device in instance.get("BlockDeviceMappings", []):
            ebs = block_device.get("Ebs")
            if not ebs:
                continue
            volume_id = ebs["VolumeId"]

            try:
                desc = ec2.describe_volumes(VolumeIds=[volume_id])
                volume = desc["Volumes"][0]
            except ClientError as e:
                print(f"ERROR: Could not describe volume {volume_id}: {e}")
                all_confirmed = False
                continue

            volume_cfn_tag = get_cfn_stack_tag(volume.get("Tags", []))
            resolved_cfn_tag = volume_cfn_tag or instance_cfn_tag

            if resolved_cfn_tag != stack_id:
                print(f"ERROR: Volume {volume_id} on instance {instance_id} "
                      f"does not confirm as part of stack '{stack_id}' "
                      f"(resolved tag: {resolved_cfn_tag}).")
                all_confirmed = False
                continue

            stack_volumes.append({
                "instance_id": instance_id,
                "instance_name": get_name_tag(instance.get("Tags", [])),
                "volume_id": volume_id,
                "device": block_device.get("DeviceName"),
                "encrypted": volume.get("Encrypted", False),
                "size_gib": volume.get("Size"),
            })

    if not all_confirmed:
        print("\n--- VALIDATION FAILED ---")
        print(f"One or more volumes in stack '{stack_id}' could not be confirmed.")
        sys.exit(1)

    print(f"\n--- VALIDATION PASSED ---")
    print(f"Stack '{stack_id}': {len(stack_volumes)} volume(s) confirmed.")
    for v in stack_volumes:
        enc = "encrypted" if v["encrypted"] else "UNENCRYPTED"
        print(f"  {v['instance_name']} ({v['instance_id']})  "
              f"{v['volume_id']}  {v['device']}  {v['size_gib']}GiB  {enc}")

    return stack_volumes


def encrypt_stack(environment, env_instance):
    stack_id = f"{environment}-{env_instance}"
    stack_volumes = validate_stack(environment, env_instance)

    instances = {}
    for v in stack_volumes:
        iid = v["instance_id"]
        if iid not in instances:
            instances[iid] = {
                "instance_name": v["instance_name"] or iid,
                "volumes": [],
            }
        instances[iid]["volumes"].append(v)

    print(f"\n--- Encrypting stack: {stack_id} ({len(instances)} instance(s)) ---")

    results = []
    with ThreadPoolExecutor(max_workers=len(instances)) as executor:
        futures = {
            executor.submit(process_instance, iid, data["instance_name"], data["volumes"]): iid
            for iid, data in instances.items()
        }
        for future in as_completed(futures):
            results.append(future.result())

    print(f"\n--- STACK ENCRYPTION SUMMARY: {stack_id} ---")
    any_failed = False
    for r in sorted(results, key=lambda x: x["instance_name"]):
        if r["status"] == "encrypted":
            print(f"  {r['instance_name']} ({r['instance_id']})  "
                  f"volumes swapped: {r['volumes_swapped']}  public ip: {r['public_ip']}")
        elif r["status"] == "skipped_already_encrypted":
            print(f"  {r['instance_name']} ({r['instance_id']})  already fully encrypted, no action taken")
        else:
            any_failed = True
            print(f"  {r['instance_name']} ({r['instance_id']})  FAILED: {r.get('error')}")

    if any_failed:
        print("\n--- ONE OR MORE INSTANCES FAILED ---")
        sys.exit(1)

    print("\n--- STACK ENCRYPTION COMPLETE ---")


def main():
    parser = argparse.ArgumentParser(description="EBS encryption pipeline")
    parser.add_argument("--stage", required=True, choices=["validate", "encrypt"],
                         help="Pipeline stage to run")
    parser.add_argument("--environment",
                         help="Environment identifier, e.g. prod, qa, uat")
    parser.add_argument("--env-instance",
                         help="Environment instance identifier, e.g. 1, 2, 3")
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)

    if args.stage == "validate":
        if not args.environment or not args.env_instance:
            print("ERROR: --environment and --env-instance are required for --stage validate")
            sys.exit(1)
        validate_stack(args.environment, args.env_instance)
    elif args.stage == "encrypt":
        if not args.environment or not args.env_instance:
            print("ERROR: --environment and --env-instance are required for --stage encrypt")
            sys.exit(1)
        encrypt_stack(args.environment, args.env_instance)


if __name__ == "__main__":
    main()