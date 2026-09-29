# aws-ebs-encryption-pipeline

Automated EBS encryption remediation for AWS stacks using Jenkins and Python.

## Background

As part of a SOC 2 / ISO compliance initiative, every EBS volume across dozens of AWS-hosted customer environments had to be encrypted at rest. Doing this by hand (snapshot, copy encrypted, create volume, stop instance, swap, restart) is slow and error-prone at that scale, so the work was split into two parts:

1. **Audit** to find every unencrypted volume and map it to its stack.
2. **Pipeline** to validate and encrypt a stack's volumes in a single, repeatable Jenkins run.

> The code in this repo has been sanitized and generalized from production work. Environment names, tags, and internal references have been replaced, so it is meant to show the approach rather than run as a drop-in tool.

## Repository layout

```
aws-ebs-encryption-pipeline/
├── audit/
│   └── ebs_encryption_audit.py   # Discovery: find unencrypted volumes across accounts and regions
└── pipeline/
    ├── Jenkinsfile               # Two-stage Jenkins pipeline: validate, then encrypt
    └── ebs_encrypt.py            # Python (boto3) logic called by each pipeline stage
```

## Audit

`audit/ebs_encryption_audit.py` is a standalone script, run locally, that scopes the work before anything is changed.

- Scans the **prod, qa, and uat** accounts across **every enabled region**, so volumes running somewhere unexpected still show up.
- Maps each volume to its attached instance, instance state, `Customer` tag, and CloudFormation stack.
- Marks each volume as **confirmed** (it resolves to a CloudFormation stack) or **unconfirmed** (it needs manual review before remediation).
- Writes a single combined JSON file (`ebs_encryption_<timestamp>.json`) with one record per volume, including account, region, encryption status, size, and stack.

The JSON output was used to plan remediation stack by stack and to track progress.

## Pipeline

`pipeline/Jenkinsfile` takes an environment, a region, and a stack instance number, and runs two stages against the stack `<environment>-<instance>`:

### 1. Validate

- Finds every EC2 instance in the stack by its `aws:cloudformation:stack-name` tag.
- Confirms each attached EBS volume belongs to that stack, using the volume's own CloudFormation tag or, if it has none, the instance's tag.
- Fails the run if any volume can't be confirmed, before any changes are made.

### 2. Encrypt

For each unencrypted volume:

1. Snapshots the original volume (`-final-unencrypted-snap`).
2. Copies the snapshot to a KMS-encrypted snapshot (`-encrypted-snap`) using the account's default `alias/aws/ebs` key.
3. Creates a new volume from the encrypted snapshot, carrying over the original's tags (Name, Customer, etc.).

Once an instance's replacement volumes are ready:

1. Stops the instance.
2. Detaches each original volume and attaches its encrypted replacement at the same device.
3. Starts the instance.
4. Waits for full AWS status checks to pass.

Instances in a stack are processed **in parallel**, independently of one another. A final per-instance summary shows the volumes swapped and each instance's new public IP.

## Design decisions

- **Tag-based stack discovery.** Stack membership comes strictly from the CloudFormation stack-name tag, not naming conventions, because instance naming was inconsistent across environments.
- **Validate before changing anything.** The encrypt stage re-runs validation first, and nothing is touched unless every volume confirms.
- **Idempotent.** Every AWS write checks for existing snapshots and volumes first, so a failed run can be re-run mid-stack without duplicating work.
- **Already-encrypted volumes are skipped.** They're detected and left untouched.
- **No deletions.** Original volumes and snapshots are kept after a successful run for manual review and rollback.
- **"Done" means healthy.** A stack only counts as complete when every instance passes full status checks, not just when it reaches `running`.

## Testing

- Verified end-to-end (unencrypted → encrypted) against a dedicated test stack.
- Built a separate cleanup pipeline that removes the encrypted volumes and snapshots and reattaches the original unencrypted volumes. This allowed the full encryption run to be repeated as many times as needed during development.

## Notes

- `internal-jenkins-lib` stands in for an internal Jenkins shared library. Its `env_parameters` step resolves the deployment IAM role and AWS account for the selected environment and region, which the pipeline uses with `withAWS` to assume the role.
- `REPO_BRANCH` is used by the Jenkins job's SCM configuration to run the pipeline from `main` or a feature branch.

## Tech

Jenkins (declarative pipeline) · Python 3 · boto3 · AWS EC2, EBS, KMS, STS, CloudFormation tagging
