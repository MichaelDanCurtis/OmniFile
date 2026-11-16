# Fixture: a customer guide in WP7's guide-step format

This is not the customer guide. It is a fixture for the harness's dry run until `docs/guide/` is
on main. It uses every `on=` the format has, `file=`, `manual=yes`, `plan=yes`, the
`gate=whatif` step (the what-if whose digest the owner approves), `cloud=aws`, and the
`<secret:…>` and `<value:…>` placeholders. It also has a step that re-creates the run's
resource group without tags, as the real guide's step 10 can. `tests/real/tests/` checks that
each step is run, written, recorded or filled as the format says.

## 1. Values

The harness writes `omnifile.env` from `omnifile.env.example`.

```bash guide-step=1 on=workstation plan=yes
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
test -n "$RG" && test -n "$LOCATION" && test -n "$PREFIX"
```

```bash guide-step=2 on=workstation
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
az group create --name "$RG" --location "$LOCATION" -o none
```

## 2. The relay's ranges

```bash guide-step=3 on=workstation plan=yes
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/relay-ranges.sh --location "$LOCATION" --out deploy/azure/relay-allowed-cidrs.json
```

## 3. Preview, then deploy what was previewed

```bash guide-step=4 on=workstation gate=whatif
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" | tee whatif-phase1.txt
echo "export OMNI_WHATIF_DIGEST='$(sed -n 's/^digest: //p' whatif-phase1.txt | tail -1)'" >> omnifile.state.env
```

```bash guide-step=5 on=workstation
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" --deploy --approve "$OMNI_WHATIF_DIGEST"
```

## 4. Images

```bash guide-step=6 on=workstation
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
# As WP7's guide: OmniFile's released images, copied into the run's registry by digest.
lock=deploy/images/images.lock.json
python3 -c 'import json,sys; l=json.load(open(sys.argv[1])); [print(v["repository"], v["digest"]) for v in l["images"].values()]' "$lock" |
while read -r repo digest; do
  az acr import --name "$OMNI_ACR" --source "$OMNI_SOURCE_REGISTRY/$repo@$digest" \
    --image "$repo:$OMNI_RELEASE" --force
  echo "imported $repo@$digest"
done
```

## 5. Entra and the Teams app

```bash guide-step=7 on=workstation
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
dom=$(az deployment group show -g "$RG" -n omnifile --query properties.outputs.internalEnvironmentDefaultDomain.value -o tsv)
web="https://$PREFIX-web.$dom"
bot=$(az deployment group show -g "$RG" -n omnifile --query properties.outputs.teamsBotAppId.value -o tsv)
zip=$(deploy/teams/build.sh --bot-app-id "$bot" --portal-url "$web" --schema "$TEAMS_SCHEMA" | tail -1)
deploy/entra/register.sh --tenant "$TENANT_ID" --prefix "$PREFIX" --web-origin "$web" \
  --web-principal "$(az deployment group show -g "$RG" -n omnifile --query properties.outputs.identityPrincipalIds.value.web -o tsv)" \
  --worker-principal "$(az deployment group show -g "$RG" -n omnifile --query properties.outputs.identityPrincipalIds.value.worker -o tsv)" \
  --teams-app-zip "$zip"
```

Grant admin consent for the worker's Graph permissions in the Entra admin center.

```bash guide-step=8 on=workstation manual=yes
# Entra admin center > Enterprise applications > id-<prefix>-worker > Permissions > Grant admin consent
```

## 6. The CA

```bash guide-step=9 on=workstation
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
az containerapp exec -n "$PREFIX-enrol" -g "$RG" --command "python -m omnifile_service.enrol init-kv-ca /tmp/ca.pem" > /dev/null
az containerapp exec -n "$PREFIX-enrol" -g "$RG" --command "cat /tmp/ca.pem" > edge-ca.pem
openssl rand -base64 32 > lease.key
az keyvault secret set --vault-name "kv-$PREFIX" -n api-lease-key --file lease.key -o none
rm -f lease.key
```

## 7. The appliance on KVM

```bash guide-step=10 on=kvm
set -euo pipefail
. ./omnifile.env
qemu-img create -f qcow2 /var/lib/libvirt/images/omni-data.qcow2 64G
virt-install --name "$EDGE_NAME" --memory 8192 --vcpus 4 --import --osinfo ubuntu24.04 \
  --boot uefi --tpm backend.type=emulator,backend.version=2.0,model=tpm-crb \
  --disk "path=appliance-files/$APPLIANCE_IMAGE,format=qcow2" --disk path=/var/lib/libvirt/images/omni-data.qcow2 \
  --network network=ofreal --graphics none --console pty,target_type=serial --noautoconsole
```

```powershell guide-step=11 on=hyperv
New-VM -Name omnifile-edge -Generation 2 -VHDPath .\omnifile-edge.vhdx -MemoryStartupBytes 8GB
```

## 8. Enrol the edge

```text guide-step=12 on=portal
open /edges
click Add edge
fill Name = <value:EDGE_NAME>
click Create
expect Enrolment code <value:ENROL_CODE>
```

```text guide-step=13 on=edge-console
<value:EDGE_NAME>
dhcp
/dev/vdb
ERASE
<value:AD_DOMAIN>
<value:AD_WORKGROUP>
<value:AD_JOIN_USER>
<secret:AD_JOIN_PASSWORD>
<value:AD_ADMIN_GROUP>
yes
<value:ENROL_CODE>
<enter>
```

```toml guide-step=14 on=edge-shell file=/etc/omnifile/extra.toml
[note]
written_by = "the guide fixture"
```

## 9. The first share

```text guide-step=15 on=portal
open /shares
click Add share
fill Name = projects
choose Protocols = NFS and SMB
click Add share
expect Share projects is ready
```

```bash guide-step=16 on=linux-client
set -euo pipefail
. ./omnifile.env
sudo mount -t nfs4 "$EDGE_NAME:/projects" /mnt/projects
ls /mnt/projects
```

## 10. AWS (migrations from S3)

```bash guide-step=17 on=workstation cloud=aws
set -euo pipefail
. ./omnifile.env
echo "aws profile=${AWS_PROFILE:-unset} region=${AWS_REGION:-unset}"
aws rolesanywhere create-trust-anchor --name "omnifile-edge-ca-$OMNIFILE_RUN_ID" --enabled \
  --source "sourceType=CERTIFICATE_BUNDLE,sourceData={x509CertificateData=$(cat "$EDGE_CA_FILE")}"
aws s3api create-bucket --bucket "ofreal-$OMNIFILE_RUN_ID-guide" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"
# Stands for someone else's work in the same account while the steps run: not the run's.
aws rolesanywhere create-trust-anchor --name someone-elses-anchor --enabled \
  --source "sourceType=CERTIFICATE_BUNDLE,sourceData={x509CertificateData=$(cat "$EDGE_CA_FILE")}"
```

## 11. Phase 2: preview and deploy again

The second deploy (the relay certificate and the CA, now made) has its own preview, whose
digest the owner approves on its own: the first approval covers the first deploy only.

```bash guide-step=18 on=workstation gate=whatif
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" | tee whatif-phase2.txt
echo "export OMNI_WHATIF_DIGEST='$(sed -n 's/^digest: //p' whatif-phase2.txt | tail -1)'" >> omnifile.state.env
```

```bash guide-step=19 on=workstation
set -euo pipefail
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" --deploy --approve "$OMNI_WHATIF_DIGEST"
```
