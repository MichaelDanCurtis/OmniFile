# OmniFile on the AWS side: IAM Roles Anywhere for the edges (setup guide)

**The service's own role** (the one it assumes with web identity, decision 43) is made by the CloudFormation template `cloudformation/omnifile-connect.yaml`, which Settings, AWS opens as "Launch in AWS" with this deployment's values (`docs/design/cloud-connect.md` section 2): the IAM OIDC provider for the tenant, the role with its trust policy and an explicit deny of every write on the source buckets (decision 40), the Batch Operations role, the ops bucket, and the S3 event queue and rule. Optionally the S3, STS and SQS interface endpoints. After it, turn on EventBridge on each source bucket, then press Fetch in the portal and set the entry it shows in `awsAccounts`. The service's packaged copy (`service/src/omnifile_service/settings/cfn/`) must stay byte-identical; a test checks. This guide is for the edges' role.

What the client sets up in its AWS account so that OmniFile edges read S3-resident files during a migration with **no keys anywhere** (decision 43). Each edge's Roles Anywhere certificate comes from **OmniFile's own per-deployment edge CA** by default (decision 50). The edge's key is sealed in its TPM. Design and threat model: `docs/design/threat-model.md` T4, `docs/contracts/control-api.md` §2–3, `docs/design/setup-wizard.md` ("Keys").

Nothing here is applied by OmniFile. The client runs these steps with their own AWS tools. The policy files in `iam/` hold placeholders in capitals (`ACCOUNT_ID`, `REGION`, `TRUST_ANCHOR_ID`, …) to replace. They were written against AWS's documentation; the items still to try on real AWS are in `docs/milestones/real-aws-checks.md`.

## 1. The interface endpoints (decision 22)

In the VPC the edges reach over Direct Connect or VPN, create interface endpoints for:

- **S3** (`com.amazonaws.REGION.s3`): the edges' reads. Its id is `S3_INTERFACE_ENDPOINT_ID` below.
- **IAM Roles Anywhere** (`com.amazonaws.REGION.rolesanywhere`): the edges' `CreateSession`, and the service's CRL upload (step 6). With private DNS on, the regional name resolves to it. Otherwise give its `https://` address in the setup wizard (`[aws] endpoint`) and in the service's settings (`endpoints.rolesanywhere`).

Interface endpoints don't offer TLS 1.3; the edge and the service allow TLS 1.2.

## 2. The trust anchor: OmniFile's edge CA

The edge CA certificate is the one made at the service's CA bootstrap (`deploy/azure/README.md`, step 7: `init-kv-ca`, the `edgeCaCertPem` parameter). It is public. Its key never leaves Key Vault.

```
aws rolesanywhere create-trust-anchor --name omnifile-edge-ca --enabled \
  --source "sourceType=CERTIFICATE_BUNDLE,sourceData={x509CertificateData=$(cat edge-ca.pem)}"
```

Note its ARN (`…:trust-anchor/TRUST_ANCHOR_ID`). The same CA issues the edges' mTLS certificates, so the role's trust policy below must tell the two apart. It does so by the subject's OU.

## 3. The role the edges read as

Create a role (for example `omnifile-edge-s3-read`) with:

- **Trust policy:** `iam/edge-role-trust.json`. Only Roles Anywhere, through this trust anchor (`aws:SourceArn`), may assume it. Only for a certificate whose subject is `OU=omnifile-roles-anywhere`: an edge's mTLS certificate has no OU, so it gets nothing. And only for the edges named (`CN=omnifile-edge-<id>`, the edge ids from the portal's Edges page): one edge's certificate can't act for an edge not listed. Add each new edge's CN when it is enrolled. A broader `StringLike` on `omnifile-edge-*` admits every edge of this deployment's CA, which is the alternative when edges come and go often.
- **Permissions:** `iam/edge-role-policy.json`. `s3:GetObject` and `s3:GetObjectVersion` on the registered source buckets and prefixes only, and only through the S3 interface endpoint (`aws:SourceVpce`). Everything off that endpoint is denied. Every write, delete, tagging, retention, restore and ACL action is denied outright (decision 40: OmniFile never writes to a source).

## 4. The profile

```
aws rolesanywhere create-profile --name omnifile-edges --enabled \
  --role-arns arn:aws:iam::ACCOUNT_ID:role/omnifile-edge-s3-read \
  --duration-seconds 3600
```

`--duration-seconds` is at most 3600. A session lasts at most an hour, so a copied session, or one an edge holds when it is revoked, ends within the hour (T4). The setup wizard's own session length (`[aws] session_seconds`) is also limited to 900 to 3600 s.

## 5. The edges

On each edge, the setup wizard's last question takes the trust anchor, profile and role ARNs (`[aws]`). It asks whether OmniFile's edge CA should issue the certificate; the default is yes. The `aws` step seals a new key to the TPM. Once the edge is enrolled, the step gets the edge's certificate from the service over mTLS: `OU=omnifile-roles-anywhere, CN=omnifile-edge-<id>`, 30 days. Then it starts the signing helper. The edge renews the certificate with its mTLS one. Nothing is signed or copied by hand.

## 6. Revocation reaches AWS: the CRL upload

Revoking an edge in the portal (Edges, Revoke) refuses its mTLS certificate at once. Its Roles Anywhere certificates go into the edge CA's CRL, which the service's worker uploads to the trust anchor: `ImportCrl` the first time, `UpdateCrl` after. It refreshes the CRL weekly too. Roles Anywhere refuses a CRL that revokes nothing, on both calls (real AWS, 2026-11-14). So once an edge has been revoked the CRL never goes back to empty: when every revoked edge's certificates have expired, it keeps one serial, the last certificate of the edge revoked most recently, and the weekly refresh goes on. Roles Anywhere accepts a CRL whose only entry is a stale serial, one no live certificate has (verified on real AWS: `UpdateCrl`, run rr-202611150718-62e9). The worker never uploads a CRL with no entries. The service can't delete a CRL (no `DeleteCrl` in its policy). For that:

- **The service's role** (the one it assumes with web identity, decision 43: `role_arn` in `OMNIFILE_AWS_ACCOUNTS`) gets `iam/service-crl-policy.json`. It may import a CRL only when it is tagged `omnifile-trust-anchor = TRUST_ANCHOR_ID` (the worker always tags it so), update only CRLs carrying that tag, and list CRLs (`ListCrls` takes no resource, so it is `*`; it lets a retry find a CRL whose import answer was lost). IAM can't tie `ImportCrl` to a trust anchor by resource, so the tag is the scope. `rolesanywhere:TagResource` is there for the tag on create (VERIFY that it is needed). Nothing else: no trust anchor, profile or role changes, and no `iam:` actions.
- **The service's settings** name the trust anchor, never a partner's input. In the account's entry of `OMNIFILE_AWS_ACCOUNTS`:

  ```json
  "roles_anywhere_trust_anchor_arn": "arn:aws:rolesanywhere:REGION:ACCOUNT_ID:trust-anchor/TRUST_ANCHOR_ID",
  "endpoints": {"rolesanywhere": "https://vpce-….rolesanywhere.REGION.vpce.amazonaws.com", "…": "…"}
  ```

  With private networking, the Roles Anywhere endpoint is required, and must be an interface endpoint. `roles_anywhere_crl_name` (default `omnifile-edge-ca`) names the CRL.
- **If the upload keeps failing,** the worker retries with backoff (30 s up to 15 min). After 15 minutes the alert `roles-anywhere-crl-failing` fires (`deploy/azure/modules/app-metrics.bicep`). Until the upload succeeds, a revoked edge's certificate still gets sessions.
- **The window:** once the CRL is in, the edge gets no new session. A session it already holds runs out within the hour (step 4). To end those at once, add to the edge role a deny of everything for sessions issued before now. IAM's "Revoke active sessions" does this:

  ```json
  {"Effect": "Deny", "Action": "*", "Resource": "*",
   "Condition": {"DateLessThan": {"aws:TokenIssueTime": "<now, ISO 8601>"}}}
  ```

  That also ends the other edges' current sessions; they get new ones on their next call.

## The client's own CA instead

A client whose policy requires its own CA (AWS Private CA or theirs) registers that CA as the trust anchor in step 2. It answers no to the wizard's CA question (`[aws] issuer = "client"`). The edge then writes its certificate request to `/etc/omnifile/roles-anywhere.csr`; the client's CA signs it, and `omnifile-setup aws-cert FILE` installs the certificate. That CA renews the certificate and revokes it (its own CRL import), and the service's CRL upload isn't used: leave `roles_anywhere_trust_anchor_arn` unset. Keep the trust policy's subject conditions on the names that CA issues.
