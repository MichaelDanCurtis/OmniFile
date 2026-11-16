# A broken guide (test fixture for docs/guide/check.sh)

Every block below breaks one rule on purpose; `test_guide_lint.py` checks that the lint names each one.
It has no Troubleshooting heading and no Kerberos numbers, which the lint also reports.

## Deploy the service: phase 1

A parameter that main.bicep doesn't have, in prose: `apiIngresss`. An output that doesn't exist is read below.
A path that doesn't exist: `deploy/azure/no-such-script.sh`.

```bash guide-step=1 on=workstation
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/relay-ranges.sh --location "$OMNI_LOCATION" --no-such-flag x
```

```bash guide-step=1 on=workstation
. ./omnifile.env && . ./omnifile.state.env
echo "a duplicate step number"
```

```bash guide-step=3 on=workstation
echo "reads $OMNI_RG without loading omnifile.env"
```

```bash guide-step=2 on=workstation
. ./omnifile.env && . ./omnifile.state.env
echo "comes after step 3, and reads $OMNI_NO_SUCH_VALUE"
az deployment group show -g "$OMNI_RG" -n omnifile --query properties.outputs.noSuchOutput.value
```

```bash guide-step=4 on=moon
echo "an unknown place"
```

```powershell guide-step=5 on=workstation
Write-Output "a workstation step in the wrong language"
```

```bash guide-step=6 on=edge-shell colour=blue
sudo omnifile-setup frobnicate
sudo omnifile-setup apply --no-such-option
```

```bash guide-step=7 on=workstation
. ./omnifile.env && . ./omnifile.state.env
uv run --project service python -m omnifile_service.enrol make-coffee
uv run --project service python -m omnifile_service.no_such_module
psql "host=x" -v prefix=x -v unused=y -f docs/guide/sql/create-principals.sql
cat >edge-extra.toml <<EOF
[aws]
trust_anchor_arn = "$OMNI_RG"
colour = "blue"
[weather]
sunny = true
EOF
```

```text guide-step=8 on=portal
open /no-such-page
click No Such Button Anywhere
wave at the screen
```

```text guide-step=9 on=edge-console
<secret:OMNI_UNDECLARED_PASSWORD>
```

```bash guide-step=10 on=edge-shell
ssh -t admin@filer 'sudo omnifile-setup nosuchcmd'
bash -c "deploy/azure/relay-ranges.sh --bogus-flag x"
sudo cat /var/lib/omnifile/no-such-file.conf
systemctl is-active omnifile-nosuch smbd
journalctl -u nfs-ganesha-nope --since '-1 min'
```

```bash guide-step=11 on=workstation
. ./omnifile.env && . ./omnifile.state.env
OMNIFILE_NO_SUCH_SETTING=1 uv run --project service python -m omnifile_service.catalog.migrate
deploy/azure/whatif.sh -g "$OMNI_RG" --no-such-whatif-flag
uv run --project service python -m omnifile_service.admin backend add azure --no-such-admin-flag x
```

```text guide-step=12 on=portal
click Recall Some Totally Made Up Thing That Is Long
click Recall ahead of tme
expect Totally Absent Text
```

```bash guide-step=13 on=workstation
. ./omnifile.env && . ./omnifile.state.env
uv run --project service --no-dev python -m omnifile_service.catalog.migrate \
  --lieve x
uv run --project service --no-dev python -m omnifile_service.admin backend add azure \
  --live x --lieve y
```

```bash guide-step=14 on=edge-shell
set -euo pipefail
sudo cat /etc/omnifile/setup.tom
```

```bash guide-step=15 on=workstation gate=maybe
. ./omnifile.env && . ./omnifile.state.env
echo "a gate of an unknown kind, which records no digest"
```

```bash guide-step=16 on=workstation plan=yes
. ./omnifile.env && . ./omnifile.state.env
echo "a plan step after the gate"
```

```bash guide-step=17 on=workstation
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$OMNI_RG" -p docs/guide/customer.bicepparam --deploy --approve "$OMNI_WHATIF_DIGEST"
```

```bash guide-step=18 on=workstation
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$OMNI_RG" -p docs/guide/customer.bicepparam \
  --deploy --approve "$OMNI_WHATIF_DIGEST"
```

```bash guide-step=19 on=workstation
. ./omnifile.env && . ./omnifile.state.env
sfx=${OMNI_AWS_NAME_SUFFIX:+-$OMNI_AWS_NAME_SUFFIX}
aws iam get-role --role-name "omnifile-service$sfx" >/dev/null
aws iam get-role --role-name omnifile-batch >/dev/null
echo "arn:aws:iam::$OMNI_AWS_ACCOUNT_ID:role/omnifile-edge-s3-read"
```

Step 1 creates `edge-ca.pem`, which it doesn't; step 999 doesn't exist.

<!-- pending WP8: no-such-feature -->
This line describes work no pending file declares.

Recall a 184 GB file in about 20 hours.

Prizmanyx then registers its sources.
