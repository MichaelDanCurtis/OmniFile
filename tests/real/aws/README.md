# The AWS run's IAM: a template the owner deploys, and the user's policy

The first real AWS run uses the IAM user `omnifile-harness-user` in account 111122223333,
which already has AmazonS3FullAccess, AmazonEC2FullAccess, ViewOnlyAccess and SecurityAudit.
Two files cover what else `tests/real/run.sh all --cloud aws` needs. Both are the owner's
steps, made once from their own admin session; the harness never changes IAM outside its run.

**1. `harness-roles.yaml`** (CloudFormation). Read it, then deploy it:

```sh
aws cloudformation deploy --region us-west-2 --stack-name omnifile-real-harness-roles \
  --template-file tests/real/aws/harness-roles.yaml --capabilities CAPABILITY_NAMED_IAM
```

It makes, under the IAM path `/omnifile-real-base/`:
- `omnifile-real-batch-ops`, the role S3 Batch Operations runs restores and checksums as
  (trusts only `batchoperations.s3.amazonaws.com` from this account);
- `omnifile-real-probe`, the probe instance's role and instance profile (trusts only EC2; runs
  the SSM agent, reads the runs' buckets and queue, creates Batch Operations jobs, and passes
  the batch role to Batch Operations only);
- `omnifile-real-base-boundary`, the boundary on both roles;
- `omnifile-real-edge-boundary`, the boundary every role the harness's user makes must carry:
  `s3:GetObject` and `s3:GetObjectVersion` on `ofreal-rr-*` objects, nothing else.

Every bucket and queue those roles reach is named `ofreal-rr-*`. Remove it all with
`aws cloudformation delete-stack --region us-west-2 --stack-name omnifile-real-harness-roles`.

**1b. Roles Anywhere's service-linked role**, once per account:

```sh
aws iam create-service-linked-role --aws-service-name rolesanywhere.amazonaws.com
```

The first trust anchor in an account makes this role (`AWSServiceRoleForRolesAnywhere`), and
that needs `iam:CreateServiceLinkedRole`, which the harness's user is not given. The first
real run stopped at `CreateTrustAnchor` for it, after it had made its buckets, queue and
endpoints; the run now checks for the role before it makes anything. "Has been taken" means
the account already has it, which is fine. It grants Roles Anywhere only its own CloudWatch
metrics and ACM Private CA reads; remove it with `aws iam delete-service-linked-role
--role-name AWSServiceRoleForRolesAnywhere` when no trust anchor is left.

**2. `harness-policy.json`**, attached to the user as two managed policies. It is about
8,500 characters, and IAM allows a user 2,048 of inline policy and 6,144 in one managed
policy, so `put-user-policy` refuses it (`LimitExceeded`; the first real run found this).
`harness-policy-part1.json` and `harness-policy-part2.json` hold the same statements in the
same order (`split_policy.py` writes them, and a test fails when they are stale). A Deny in
one applies whichever allows, so the two together decide exactly as the one file does. Read
`harness-policy.json`, then:

```sh
for n in 1 2; do
  aws iam create-policy --policy-name omnifile-real-harness-$n \
    --policy-document file://tests/real/aws/harness-policy-part$n.json
  aws iam attach-user-policy --user-name omnifile-harness-user \
    --policy-arn arn:aws:iam::111122223333:policy/omnifile-real-harness-$n
done
```

When a harness update changes the policy, replace both in place (IAM keeps five versions of
a managed policy; delete an old one with `delete-policy-version` when it refuses a sixth):

```sh
for n in 1 2; do
  aws iam create-policy-version --set-as-default \
    --policy-arn arn:aws:iam::111122223333:policy/omnifile-real-harness-$n \
    --policy-document file://tests/real/aws/harness-policy-part$n.json
done
```

The user then has six managed policies of the ten IAM allows. Remove them after the run:

```sh
for n in 1 2; do
  aws iam detach-user-policy --user-name omnifile-harness-user \
    --policy-arn arn:aws:iam::111122223333:policy/omnifile-real-harness-$n
  aws iam delete-policy --policy-arn arn:aws:iam::111122223333:policy/omnifile-real-harness-$n
done
```

Exactly what it grants, beyond the managed policies:

- **IAM.** It can read the template's roles, profile and boundaries, and pass
  `omnifile-real-batch-ops` to `batchoperations.s3.amazonaws.com` and `omnifile-real-probe`
  to `ec2.amazonaws.com`, nothing else. It may make, give an inline policy to and delete roles
  under `/omnifile-real/` only if they carry `omnifile-real-edge-boundary` (each run's edge
  role, from `deploy/aws/iam/`), tag them with `omnifile-harness=tests-real` and an
  `omnifile-run` of `rr-*` when it makes them, and pass them to Roles Anywhere for the run's
  profile. It may list roles and instance profiles.
- **IAM, denied.** Any role made, changed or deleted without the edge boundary; adding,
  changing or removing any permissions boundary; any write to the template's roles, profile
  or boundaries. No managed policy can be attached anywhere.
- **SQS and EventBridge.** Queues and rules named `ofreal-rr-*` in us-west-2: made only with
  the tag `omnifile-harness=tests-real`, used and deleted only when they carry it.
- **Roles Anywhere.** Trust anchors, profiles and CRLs made only with the tags
  `omnifile-harness=tests-real` and `omnifile-run=rr-*` (and no tag keys but those and
  `omnifile-expires`); `rolesanywhere:TagResource` under the same conditions, since a create
  that carries tags needs it too; read, CRL-updated and deleted only when they carry both
  tags. Listing is allowed. Updating an anchor or profile is never allowed.
- **SSM.** `AWS-RunShellScript` only, on instances tagged both `omnifile-harness=tests-real`
  and `omnifile-run=rr-*`; reading command results; reading the public Amazon Linux AMI
  parameter.
- **KMS.** `kms:GenerateDataKey` and `kms:Decrypt` only through S3 in us-west-2 and only for
  an `ofreal-rr-*` bucket's encryption context; `kms:DescribeKey` through S3.
- **Tags.** Reading tags across the account (`tag:GetResources`), for the teardown proof.
- **Tags, denied.** No `omnifile-*` tag can be put on an EC2 resource that already exists:
  only as part of `RunInstances`, `CreateVpc`, `CreateSubnet`, `CreateSecurityGroup` or
  `CreateVpcEndpoint`. None through `ssm:AddTagsToResource` or `tag:TagResources`; on queues,
  rules and roles, only the run's own. No `omnifile-*` tag can be removed from anything. So
  the tags `ssm:SendCommand` trusts can't be forged onto the owner's instances.

Each run's own buckets come under AmazonS3FullAccess, and its VPC, endpoints and probe under
AmazonEC2FullAccess; the policy adds nothing there except the tag Deny.

**The one trade-off: Roles Anywhere tags.** A create that carries tags is also checked as
`rolesanywhere:TagResource`, and IAM can't tell that check from a `TagResource` on a trust
anchor, profile or CRL that already exists (neither has a resource tag, and Roles Anywhere's
ARNs hold no name to scope by). So the user can put the run's tags on an existing Roles
Anywhere resource of the owner's, and then read it, delete it, or replace its CRL (which
could un-revoke a certificate). It can't reach the owner's roles that way: updating an anchor
or profile is never allowed, and the only roles it can give a profile are its own
edge-bounded ones. Separately, and older than this policy's tag rules: `ImportCrl` names
its trust anchor in the request and has no resource to scope by, so the user could attach a
CRL to an existing owner anchor without tagging it (revoking the owner's certificates, not
gaining any). With no Roles Anywhere resources in the account, as here, neither matters.
Before attaching the policy, check the account has no Roles Anywhere
resources of its own (`aws rolesanywhere list-trust-anchors --region us-west-2` and
`list-profiles` empty, as expected for this account). If it has some, don't attach the
policy there: run the AWS harness in an account without them.

**Check the policy before attaching it.** IAM's policy simulator evaluates it without
changing anything (`iam:SimulateCustomPolicy`, a read the owner's session has):

```bash
# An array, so it works the same in bash and zsh (zsh doesn't split a plain $TAGS).
P=file://tests/real/aws/harness-policy.json
TAGS=(
  'ContextKeyName=aws:RequestTag/omnifile-harness,ContextKeyValues=tests-real,ContextKeyType=string'
  'ContextKeyName=aws:RequestTag/omnifile-run,ContextKeyValues=rr-1,ContextKeyType=string'
  'ContextKeyName=aws:TagKeys,ContextKeyValues=omnifile-harness,omnifile-run,omnifile-expires,ContextKeyType=stringList'
)
# the create path: each create, and the TagResource it needs: all "allowed"
aws iam simulate-custom-policy --policy-input-list "$P" --context-entries "${TAGS[@]}" \
  --action-names rolesanywhere:CreateTrustAnchor rolesanywhere:CreateProfile \
    rolesanywhere:ImportCrl rolesanywhere:TagResource \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
# an owner's existing trust anchor, with the run's tags forged onto it: TagResource is
# "allowed" (the trade-off above), the updates "implicitDeny"
A=arn:aws:rolesanywhere:us-west-2:111122223333:trust-anchor/00000000-0000-0000-0000-000000000000
aws iam simulate-custom-policy --policy-input-list "$P" --resource-arns "$A" \
  --context-entries "${TAGS[@]}" \
    'ContextKeyName=aws:ResourceTag/omnifile-harness,ContextKeyValues=tests-real,ContextKeyType=string' \
    'ContextKeyName=aws:ResourceTag/omnifile-run,ContextKeyValues=rr-1,ContextKeyType=string' \
  --action-names rolesanywhere:TagResource rolesanywhere:UpdateTrustAnchor \
    rolesanywhere:UpdateProfile rolesanywhere:PutNotificationSettings \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
# a tag forged onto an existing instance: "explicitDeny"
aws iam simulate-custom-policy --policy-input-list "$P" \
  --resource-arns arn:aws:ec2:us-west-2:111122223333:instance/i-0123456789abcdef0 \
  --context-entries "${TAGS[@]}" --action-names ec2:CreateTags \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
```

`test_aws_policy.py` gives the same verdicts from its own evaluator of the policy.

**What the user already has is itself powerful.** AmazonEC2FullAccess lets it, for example,
change the user data of a stopped instance that runs with an admin instance profile, or start
an instance with any profile it can pass. This policy adds nothing to that, and passes no role
but the probe's to EC2; narrowing the managed policies is worth doing whatever this policy does.

The cheap first pass is `tests/real/run.sh all --cloud aws --scope cheap`, and the plan prints
what it should cost (about $0.57 at us-west-2 list prices for an hour's run; the figures are in
`evidence/plan.json`). To keep it that low it:

- makes six interface endpoints, not eight: S3, SQS, Roles Anywhere and the three the probe needs
  for SSM, but no STS or S3 Control endpoint (the probe has no internet path, so SSM's three stay);
- runs the probe as a t3.micro (the smallest type an account on AWS's free plan may start), and
  terminates it as soon as the checks that use it are done;
- uses objects of KB to MB (5 MiB parts), and leaves out RW-02's 5.2 GiB object;
- restores from Flexible Retrieval at Bulk only, and runs one Batch Operations restore wave
  instead of two (each job costs $0.25, the biggest item);
- expires after 3 hours unless `--ttl-hours` says otherwise.

The restore timings in RW-09, including Deep Archive's and the 1.2 TB one, are manual lines and
never run here. The scope is part of the approved plan: a run planned cheap can't be continued
as full.

`tests/real/tests/test_aws_policy.py` fails when the run calls an action the policy and those
two managed policies don't cover, when a mutating statement is left unscoped, or when any of
the review's escalation routes is allowed (a self-made role without the edge boundary, a
probe-named role passed to EC2, a forged tag, `kms:Decrypt` for another bucket's object).
