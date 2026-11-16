#!/usr/bin/env bash
# The service's AWS settings (OMNIFILE_AWS_ACCOUNTS) for one account (docs/guide/README.md, step 75).
#
#   docs/guide/aws/accounts-json.sh >aws-accounts.json
#
# Builds the JSON the service reads (service/src/omnifile_service/backends/s3/config.py, AwsAccount) from
# the values the guide recorded: the four interface endpoints, the service and Batch roles, the event
# queue, the work bucket and the trust anchor. With private networking every endpoint is an interface
# endpoint's own name (*.vpce.amazonaws.com), and nothing falls back to a public one (decision 22). It
# prints the JSON on one line, and calls nothing. The role names end in -OMNI_AWS_NAME_SUFFIX in a test run.
set -euo pipefail

need() {
  for n in "$@"; do
    [ -n "${!n:-}" ] || { echo "accounts-json.sh: $n is not set (section 17)" >&2; exit 1; }
  done
}
need OMNI_AWS_ACCOUNT_ID OMNI_AWS_REGION OMNI_AWS_OPS_BUCKET OMNI_AWS_VPCE_S3_DNS OMNI_AWS_VPCE_STS_DNS \
  OMNI_AWS_VPCE_SQS_DNS OMNI_AWS_VPCE_ROLESANYWHERE_DNS OMNI_AWS_EVENT_QUEUE_URL OMNI_AWS_TRUST_ANCHOR_ARN

jq -nc \
  --arg account "$OMNI_AWS_ACCOUNT_ID" --arg region "$OMNI_AWS_REGION" --arg ops "$OMNI_AWS_OPS_BUCKET" \
  --arg s3 "$OMNI_AWS_VPCE_S3_DNS" --arg sts "$OMNI_AWS_VPCE_STS_DNS" --arg sqs "$OMNI_AWS_VPCE_SQS_DNS" \
  --arg ra "$OMNI_AWS_VPCE_ROLESANYWHERE_DNS" --arg queue "$OMNI_AWS_EVENT_QUEUE_URL" \
  --arg ta "$OMNI_AWS_TRUST_ANCHOR_ARN" --arg sfx "${OMNI_AWS_NAME_SUFFIX:+-$OMNI_AWS_NAME_SUFFIX}" \
  '{($account): {
      region: $region,
      endpoints: {
        s3: "https://bucket.\($s3)",
        s3_control: "https://control.\($s3)",
        sts: "https://\($sts)",
        sqs: "https://\($sqs)",
        rolesanywhere: "https://\($ra)"
      },
      role_arn: "arn:aws:iam::\($account):role/omnifile-service\($sfx)",
      event_queues: {"*": $queue},
      batch: {role_arn: "arn:aws:iam::\($account):role/omnifile-batch\($sfx)", manifest_bucket: $ops},
      staging_bucket: $ops,
      roles_anywhere_trust_anchor_arn: $ta
    }}'
