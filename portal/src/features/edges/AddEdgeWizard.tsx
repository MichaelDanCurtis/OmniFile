import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { api, unwrap } from "../../api/client";
import { useAddEdge, useEdges } from "../../api/queries";
import type { components } from "../../api/schema.gen";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { CheckIcon, LockIcon } from "../../components/icons";
import { formatDateTime, formatRelative } from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";
import "./wizard.css";

type Bundle = components["schemas"]["EnrolmentBundle"];
type Appliance = components["schemas"]["EdgeAppliance"];

/** How often the last step looks for the new edge's enrolment and first heartbeat. */
export const ENROL_POLL_MS = import.meta.env.MODE === "test" ? 200 : 3000;

const NAME = /^[a-z][a-z0-9-]{0,62}$/;

type PlatformId = "hyperv" | "vmware" | "kvm" | "azure" | "aws";

interface Platform {
  id: PlatformId;
  name: string;
  format: string;
  blurb: string;
  /** No image is built for it: shown, but can't be chosen. */
  locked?: string;
}

/** The platforms appliance/convert.sh writes an image for, and AWS, which has none yet. */
export const PLATFORMS: Platform[] = [
  { id: "hyperv", name: "Hyper-V", format: "VHDX", blurb: "A Generation 2 VM (UEFI)." },
  {
    id: "vmware",
    name: "VMware vSphere",
    format: "OVA",
    blurb: "ESXi 7.0 U2 or later (virtual hardware 17).",
  },
  {
    id: "kvm",
    name: "KVM or Proxmox",
    format: "qcow2",
    blurb: "libvirt with virt-install, or Proxmox VE.",
  },
  {
    id: "azure",
    name: "Azure VM",
    format: "VHD",
    blurb: "A fixed-size VHD for a Generation 2 VM with the Standard security type.",
  },
  {
    id: "aws",
    name: "AWS EC2",
    format: "AMI",
    blurb: "An EC2 instance.",
    locked: "No AWS image is built yet: the release makes none for EC2.",
  },
];

const STEPS = ["Platform", "Image", "Requirements", "Enrol", "Online"] as const;

function useAppliance(enabled: boolean) {
  return useQuery({
    queryKey: ["edges", "appliance"],
    enabled,
    queryFn: () => unwrap(api.GET("/v1/edges/appliance")),
  });
}

function Stepper({ at }: { at: number }) {
  return (
    <ol
      className="steps"
      aria-label="Add an edge"
      style={{ gridTemplateColumns: `repeat(${STEPS.length}, minmax(0, 1fr))` }}
    >
      {STEPS.map((name, i) => {
        const status = i < at ? "done" : i === at ? "current" : "pending";
        return (
          <li
            key={name}
            className="step"
            data-status={status}
            aria-current={status === "current" ? "step" : undefined}
          >
            <span className="step-dot" aria-hidden="true">
              {status === "done" ? <CheckIcon /> : i + 1}
            </span>
            <span className="step-name">{name}</span>
          </li>
        );
      })}
    </ol>
  );
}

/** A block of commands, with a copy button. */
function Commands({ label, children }: { label: string; children: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <figure className="cmd">
      <figcaption className="cmd-head">
        <span>{label}</span>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => {
            if (!("clipboard" in navigator)) return;
            void navigator.clipboard
              .writeText(children)
              .then(() => setCopied(true))
              .catch(() => undefined);
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </figcaption>
      {/* Focusable so a keyboard can scroll a long line (axe: scrollable-region-focusable). */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <pre className="cmd-body" tabIndex={0}>
        <code>{children}</code>
      </pre>
    </figure>
  );
}

function fileFor(a: Appliance | undefined, p: PlatformId) {
  return a?.images.find((i) => i.platform === p);
}

const EXT: Record<string, string> = { hyperv: "vhdx", vmware: "ova", kvm: "qcow2", azure: "vhd" };

function ImageStep({ platform, appliance }: { platform: Platform; appliance: Appliance }) {
  const img = fileFor(appliance, platform.id);
  const file = img?.file ?? `omnifile-edge_<release>.${EXT[platform.id] ?? ""}`;
  return (
    <div className="stack">
      <h2>Get the {platform.name} image</h2>
      <dl className="kv card card-pad">
        <dt>File</dt>
        <dd className="mono">{file}</dd>
        <dt>Release</dt>
        <dd>
          {appliance.release ??
            "This service isn't a release build: use the release folder's version."}
        </dd>
        <dt>Where</dt>
        <dd>
          {img?.url ? (
            <a href={img.url} rel="noreferrer">
              Download {img.file}
            </a>
          ) : (
            <>
              In the release folder OmniFile gives you, under <code>appliance-files/</code>, with a{" "}
              <code>SHA256SUMS</code> file that lists each image.
            </>
          )}
        </dd>
        {appliance.checksums ? (
          <>
            <dt>Checksums</dt>
            <dd>
              <a href={appliance.checksums} rel="noreferrer">
                SHA256SUMS
              </a>
            </dd>
          </>
        ) : null}
      </dl>
      {!img?.url ? (
        <Callout title="Not published for download">
          A release builds the images with <code>appliance/convert.sh</code> and names each one{" "}
          <code>omnifile-edge_&lt;release&gt;.&lt;vhdx|ova|qcow2|vhd&gt;</code>. Today a release
          publishes only the update feed online, so the images come in the release folder. When your
          operator publishes them, they set <code>OMNIFILE_APPLIANCE_IMAGES_URL</code> and this step
          links to each file.
        </Callout>
      ) : null}
      <p>Check the file against SHA256SUMS before you import it:</p>
      <Commands label="Check the image">{`sha256sum --check --ignore-missing SHA256SUMS`}</Commands>
      <ImportSteps platform={platform.id} file={file} />
    </div>
  );
}

function ImportSteps({ platform, file }: { platform: PlatformId; file: string }) {
  if (platform === "hyperv") {
    return (
      <>
        <h3>Create the VM</h3>
        <p>On the Hyper-V host, in PowerShell as an administrator. Set the first lines first.</p>
        <Commands label="Hyper-V (customer guide 11.1)">{`$Name    = 'omnifile-hou1'                 # the filer's name
$Switch  = 'LAN'                           # the switch your users' network is on
$Dir     = 'D:\\Hyper-V\\omnifile-hou1'     # where this VM's disks go
$DataGB  = 2048                            # the local cache

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Copy-Item ".\\appliance-files\\${file}" "$Dir\\system.vhdx"
Resize-VHD -Path "$Dir\\system.vhdx" -SizeBytes 64GB
New-VHD -Path "$Dir\\data.vhdx" -SizeBytes ($DataGB * 1GB) -Dynamic | Out-Null
New-VM -Name $Name -Generation 2 -MemoryStartupBytes 8GB -VHDPath "$Dir\\system.vhdx" -SwitchName $Switch | Out-Null
Set-VMMemory -VMName $Name -DynamicMemoryEnabled $false
Set-VMProcessor -VMName $Name -Count 4
Set-VMFirmware -VMName $Name -EnableSecureBoot Off
Add-VMHardDiskDrive -VMName $Name -Path "$Dir\\data.vhdx"
Set-VMKeyProtector -VMName $Name -NewLocalKeyProtector
Enable-VMTPM -VMName $Name
Set-VM -Name $Name -AutomaticCheckpointsEnabled $false
Disable-VMIntegrationService -VMName $Name -Name 'Time Synchronization'
Start-VM -Name $Name
vmconnect.exe localhost $Name`}</Commands>
        <p className="muted">
          Time synchronisation is off because the filer takes its time from your domain controllers.
          Automatic checkpoints are off: a restored checkpoint would bring back a filer the service
          no longer agrees with.
        </p>
      </>
    );
  }
  if (platform === "vmware") {
    return (
      <>
        <h3>Create the VM</h3>
        <p>
          With <code>govc</code> on a machine that reaches vCenter. In the vSphere Client the same
          import is <strong>Deploy OVF Template</strong>.
        </p>
        <Commands label="vSphere (customer guide 11.2)">{`export GOVC_URL='https://vcenter.corp.example' GOVC_USERNAME='administrator@vsphere.local'
export GOVC_DATACENTER='DC1' GOVC_DATASTORE='datastore1' GOVC_NETWORK='VM Network' GOVC_RESOURCE_POOL='*/Resources'
name='omnifile-hou1'; data_gb=2048
read -rsp 'vCenter password: ' GOVC_PASSWORD && export GOVC_PASSWORD && echo

govc import.spec "appliance-files/${file}" |
  jq --arg net "$GOVC_NETWORK" '.NetworkMapping[0].Network = $net | .PowerOn = false' >ova-options.json
govc import.ova -options=ova-options.json -name="$name" "appliance-files/${file}"
govc vm.disk.change -vm "$name" -disk.label 'Hard disk 1' -size 64G
govc vm.disk.create -vm "$name" -name "$name/data" -size "\${data_gb}G" -thick=false
govc vm.power -on "$name"
govc vm.console "$name"`}</Commands>
        <p className="muted">
          Add a virtual TPM before the first start if vCenter has a key provider (Edit Settings, Add
          New Device, Trusted Platform Module). Without one the filer seals its keys with a key on
          its own disk.
        </p>
      </>
    );
  }
  if (platform === "kvm") {
    return (
      <>
        <h3>Create the VM</h3>
        <p>
          On a KVM host with libvirt and virt-install, as root. The filer joins the bridge you name.
        </p>
        <Commands label="KVM (customer guide 11.3)">{`name='omnifile-hou1'; data_gb=2048; bridge='br0'
dir=/var/lib/libvirt/images
cp "appliance-files/${file}" "$dir/$name.qcow2"
qemu-img resize "$dir/$name.qcow2" 64G
qemu-img create -f qcow2 "$dir/$name-data.qcow2" "\${data_gb}G"
virt-install --name "$name" --memory 8192 --vcpus 4 --cpu host-passthrough \\
  --boot uefi,firmware.feature0.name=secure-boot,firmware.feature0.enabled=no \\
  --disk "path=$dir/$name.qcow2,format=qcow2,bus=virtio" \\
  --disk "path=$dir/$name-data.qcow2,format=qcow2,bus=virtio" \\
  --network "bridge=$bridge,model=virtio" \\
  --tpm backend.type=emulator,backend.version=2.0,model=tpm-crb \\
  --osinfo linux2022 --graphics vnc,listen=127.0.0.1 --import --noautoconsole
virsh vncdisplay "$name"`}</Commands>
        <p className="muted">
          Setup asks its questions on the VM&apos;s screen, not the serial console: open it with{" "}
          <code>virt-viewer</code>. On Proxmox, import the same qcow2 as the VM&apos;s disk, with
          OVMF (UEFI), a TPM state disk and the sizes on the next step; the guide&apos;s tested
          recipe is the virt-install one.
        </p>
      </>
    );
  }
  return (
    <>
      <h3>Create the VM</h3>
      <p>
        Upload the fixed VHD as a page blob, make a managed disk from it, and start a Generation 2
        VM from that disk with the <strong>Standard</strong> security type (Trusted launch would
        need OmniFile&apos;s certificate in its database). Give it a second, empty data disk.
      </p>
      <Callout tone="warn" title="Not yet tried on a real subscription">
        appliance/convert.sh builds this VHD for Azure, and qemu boots it; booting it as an Azure VM
        is a check that still needs a subscription (docs/design/appliance-image.md).
      </Callout>
    </>
  );
}

function Requirements() {
  return (
    <div className="stack">
      <h2>Size the VM and open the network</h2>
      <div className="grid-2">
        <section className="card card-pad" aria-labelledby="req-vm">
          <h3 id="req-vm">The VM</h3>
          <dl className="kv">
            <dt>vCPUs</dt>
            <dd>4</dd>
            <dt>Memory</dt>
            <dd>8 GiB, fixed (not dynamic)</dd>
            <dt>System disk</dt>
            <dd>the appliance&apos;s disk, grown to 64 GiB before the first start</dd>
            <dt>Data disk</dt>
            <dd>a second, empty disk: the local cache the shares live on. Setup formats it.</dd>
            <dt>TPM</dt>
            <dd>
              a virtual TPM 2.0. The filer seals its keys to it; without one it uses a key on its
              own disk.
            </dd>
            <dt>Firmware</dt>
            <dd>UEFI. Secure Boot off, unless you enrolled OmniFile&apos;s signing key first.</dd>
            <dt>Network</dt>
            <dd>
              one adapter, on the network your users reach the shares from, with a fixed address
            </dd>
          </dl>
        </section>
        <section className="card card-pad" aria-labelledby="req-net">
          <h3 id="req-net">The network</h3>
          <dl className="kv">
            <dt>To Azure</dt>
            <dd>
              ExpressRoute or a site-to-site VPN into the hub the service is peered to. Storage is
              reached only through private endpoints, so this path carries every byte.
            </dd>
            <dt>Outbound</dt>
            <dd>
              your domain controllers (DNS, Kerberos, LDAP), your time source, and HTTPS (443 and
              8443) to the service&apos;s network in Azure. Nothing on the internet.
            </dd>
            <dt>Inbound</dt>
            <dd>
              from your users&apos; networks: SMB (TCP 445), NFS (TCP 2049), and for NFSv3 rpcbind
              (111), MOUNT (20048), NLM locks (32803) and statd (32765), each TCP and UDP. SSH (TCP
              22) for its AD admins. The appliance drops everything else.
            </dd>
            <dt>AD</dt>
            <dd>
              an account that may join computers, and an AD group whose members administer the
              filer. Admins need a <code>loginShell</code>, and that group a <code>gidNumber</code>.
            </dd>
          </dl>
        </section>
      </div>
      <Callout title="ExpressRoute throughput">
        Private-endpoint traffic crosses the ExpressRoute gateway (1, 2 or 10 Gbps by its size), and
        Microsoft says it &quot;might be reduced by half&quot;. Size the gateway for the data you
        will move.
      </Callout>
    </div>
  );
}

function useNow(everyMs: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(t);
  }, [everyMs]);
  return now;
}

function expiresIn(iso: string, now: number): string {
  const ms = new Date(iso).getTime() - now;
  if (ms <= 0) return "expired";
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return h > 0 ? `${h} h ${m} min left` : `${m} min left`;
}

function EnrolStep({
  platform,
  appliance,
  issued,
  onIssued,
}: {
  /** Undefined when the admin skipped to the code (customer guide section 13). */
  platform: PlatformId | undefined;
  appliance: Appliance;
  issued: Bundle | null;
  onIssued: (b: Bundle) => void;
}) {
  const add = useAddEdge();
  const [name, setName] = useState("");
  const ids = { name: useId(), hint: useId(), code: useId() };
  const valid = NAME.test(name);
  const now = useNow(30_000);
  if (!appliance.enrolment_configured) {
    return (
      <Callout tone="warn" title="Enrolment isn't configured on this service">
        Set <code>OMNIFILE_ENROL_PUBLIC_URL</code> and <code>OMNIFILE_ENROL_SERVER_CA</code> on the
        web app (deploy/azure/README.md), then come back to this step.
      </Callout>
    );
  }
  if (!issued) {
    return (
      <div className="stack">
        <h2>Name the edge and make its code</h2>
        <div className="field">
          <label className="field-label" htmlFor={ids.name}>
            Name
          </label>
          <input
            id={ids.name}
            value={name}
            aria-describedby={ids.hint}
            aria-invalid={name !== "" && !valid}
            maxLength={63}
            onChange={(e) => setName(e.target.value.trim())}
          />
          <p id={ids.hint} className="muted">
            How the portal shows this filer: lowercase letters, digits and hyphens, e.g.{" "}
            <code>houston-1</code>.
          </p>
        </div>
        {add.isError ? <ErrorCallout error={add.error} /> : null}
        <div>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!valid || add.isPending}
            onClick={() => add.mutate(name, { onSuccess: onIssued })}
          >
            {add.isPending ? "Creating…" : "Create code"}
          </button>
        </div>
      </div>
    );
  }
  const left = expiresIn(issued.expires_at, now);
  return (
    <div className="stack">
      <h2>Enrol {issued.name}</h2>
      <Callout tone="warn" title="Copy this code now: it is shown only once">
        It works once, until {formatDateTime(issued.expires_at)} ({left}). A lost or expired code is
        replaced from the Edges page (New code).
      </Callout>
      <CodeBox issued={issued} id={ids.code} />
      <h3>On the filer&apos;s console</h3>
      <ol className="console-steps">
        <li>
          Start the VM. Its console shows <strong>OmniFile edge filer setup</strong>, asking in
          plain language: the filer&apos;s name, its address, the data disk, the AD domain, the join
          account, the admin group, the join password, private endpoints (answer <code>y</code>).
        </li>
        <li>
          At <strong>The enrolment code</strong>, paste the code and press Enter.
        </li>
        <li>Answer the update and restart-window questions, read the summary, and confirm.</li>
        <li>
          Setup works through its steps, one line each, and ends with <code>enrol done</code> and{" "}
          <code>services done</code>.
        </li>
      </ol>
      <p>If setup has already run on this filer, enrol it from a shell as one of its AD admins:</p>
      <Commands label="Enrol a filer that is already set up">{`sudo omnifile-setup enrol`}</Commands>
      {platform === "kvm" ? (
        <>
          <h3>Unattended (qemu)</h3>
          <p className="muted">
            For VM templates, pass the answers and the code as systemd credentials through SMBIOS
            type 11: <code>omnifile.setup</code> (the answers file),{" "}
            <code>omnifile.join-password</code> and <code>omnifile.enrol-bundle</code>. Setup then
            runs at first boot and the console asks nothing (docs/design/setup-wizard.md,
            Unattended).
          </p>
          <Commands label="qemu arguments">{`-smbios "type=11,value=io.systemd.credential.binary:omnifile.setup=$(base64 -w0 setup.toml)" \\
-smbios "type=11,value=io.systemd.credential.binary:omnifile.enrol-bundle=$(printf '%s' '${issued.bundle}' | base64 -w0)"`}</Commands>
        </>
      ) : null}
    </div>
  );
}

function CodeBox({ issued, id }: { issued: Bundle; id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        Enrolment code for {issued.name}
      </label>
      <div className="code-box">
        <textarea
          id={id}
          className="mono"
          readOnly
          rows={3}
          value={issued.bundle}
          onFocus={(e) => e.currentTarget.select()}
        />
        <button
          type="button"
          className="btn"
          onClick={() => {
            if (!("clipboard" in navigator)) return;
            void navigator.clipboard
              .writeText(issued.bundle)
              .then(() => setCopied(true))
              .catch(() => undefined);
          }}
        >
          {copied ? "Copied" : "Copy code"}
        </button>
      </div>
    </div>
  );
}

function Waiting({ issued }: { issued: Bundle }) {
  const edges = useEdges(true, ENROL_POLL_MS);
  const edge = edges.data?.edges.find((e) => e.edge_id === issued.edge_id);
  const enrolled = edge?.state === "active";
  const heartbeat = enrolled ? edge.last_heartbeat_at : null;
  const online = heartbeat !== null;
  const stages: { name: string; done: boolean; note: ReactNode }[] = [
    { name: "Code created", done: true, note: `for ${issued.name}` },
    {
      name: "Enrolled",
      done: enrolled,
      note: enrolled
        ? "It has its certificate from the edge CA."
        : "Waiting for omnifile-setup on the filer to use the code…",
    },
    {
      name: "First heartbeat",
      done: online,
      note: online
        ? `Heard ${formatRelative(heartbeat)}.`
        : enrolled
          ? "Enrolled; waiting for its first heartbeat (every minute)…"
          : "After enrolment, the filer reports every minute.",
    },
  ];
  return (
    <div className="stack">
      <h2>{online ? `${issued.name} is online` : `Waiting for ${issued.name} to enrol`}</h2>
      <ol className="enrol-wait" aria-label="Enrolment">
        {stages.map((s, i) => {
          const current = !s.done && (i === 0 || stages[i - 1]?.done);
          return (
            <li key={s.name} data-done={s.done || undefined} data-current={current || undefined}>
              <span className="enrol-dot" aria-hidden="true">
                {s.done ? <CheckIcon /> : null}
              </span>
              <span className="enrol-name">
                {s.name}
                <span className="sr-only">
                  {s.done ? ": done" : current ? ": waiting" : ": not yet"}
                </span>
              </span>
              <span className="enrol-note">{s.note}</span>
            </li>
          );
        })}
      </ol>
      <p role="status" aria-live="polite" className="sr-only">
        {online ? `${issued.name} is online.` : enrolled ? `${issued.name} enrolled.` : ""}
      </p>
      {edges.isError ? <ErrorCallout error={edges.error} /> : null}
      {online ? (
        <Callout tone="ok" title="Done">
          The edge reports in. Next, give it a dataset and a share, or a filer for an annex.{" "}
          <Link to="/edges">See all edges</Link>.
        </Callout>
      ) : (
        <p className="muted">
          This page checks every few seconds. You can leave it: the edge shows on the Edges page as
          Not enrolled until it enrols.
        </p>
      )}
    </div>
  );
}

/** Edges, Add edge (WP18): from choosing a hypervisor to the new edge's first heartbeat. */
export function AddEdgeWizard() {
  usePageTitle("Add an edge");
  const { me } = useSession();
  const appliance = useAppliance(isArchiveAdmin(me));
  const [at, setAt] = useState(0);
  const [platformId, setPlatformId] = useState<PlatformId | "">("");
  const [issued, setIssued] = useState<Bundle | null>(null);
  const groupId = useId();
  if (!isArchiveAdmin(me)) {
    return (
      <div className="page">
        <h1>Add an edge</h1>
        <ErrorCallout error={new Error("Adding edges is for ArchiveAdmin.")} />
      </div>
    );
  }
  const platform = PLATFORMS.find((p) => p.id === platformId);
  const canNext =
    at === 0 ? !!platform && !platform.locked : at === 3 ? issued !== null : at < STEPS.length - 1;
  return (
    <div className="page">
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/edges">Edges</Link>
          </li>
          <li>
            <span aria-current="page">Add an edge</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>Add an edge</h1>
          <p className="page-question">Where will the new edge filer run, and is it enrolled?</p>
          <p className="lede">
            Five steps: the platform, its image, the VM&apos;s size and network, the one-time code
            for setup, and the first heartbeat.
          </p>
        </div>
      </div>
      <Stepper at={at} />
      <div className="card card-pad wizard-body">
        {appliance.isError ? <ErrorCallout error={appliance.error} /> : null}
        {!appliance.data ? (
          appliance.isError ? null : (
            <Loading />
          )
        ) : at === 0 ? (
          <fieldset className="options platform-grid" aria-describedby={groupId}>
            <legend>
              <h2>Where will it run?</h2>
            </legend>
            <p id={groupId} className="muted">
              One appliance build, in each hypervisor&apos;s format.
            </p>
            {PLATFORMS.map((p) => (
              <label
                key={p.id}
                className="option platform"
                data-locked={p.locked ? true : undefined}
              >
                <input
                  type="radio"
                  name="platform"
                  value={p.id}
                  checked={platformId === p.id}
                  disabled={!!p.locked}
                  onChange={() => setPlatformId(p.id)}
                />
                <span className="option-title">
                  {p.name} <span className="platform-format">{p.format}</span>
                  {p.locked ? <LockIcon aria-hidden="true" /> : null}
                </span>
                <span className="option-body">{p.locked ?? p.blurb}</span>
              </label>
            ))}
            <p className="skip-row">
              The VM already imported and set up (customer guide sections 11 and 12)?{" "}
              <button type="button" className="btn btn-link" onClick={() => setAt(3)}>
                Skip to the code
              </button>
            </p>
          </fieldset>
        ) : at === 1 && platform ? (
          <ImageStep platform={platform} appliance={appliance.data} />
        ) : at === 2 ? (
          <Requirements />
        ) : at === 3 ? (
          <EnrolStep
            platform={platform?.id}
            appliance={appliance.data}
            issued={issued}
            onIssued={setIssued}
          />
        ) : issued ? (
          <Waiting issued={issued} />
        ) : null}
      </div>
      <div className="wizard-nav">
        {at > 0 && !(at === 4) ? (
          <button
            type="button"
            className="btn"
            onClick={() => setAt(at === 3 && !platform ? 0 : at - 1)}
          >
            Back
          </button>
        ) : (
          <Link className="btn" to="/edges">
            {at === 4 ? "Back to edges" : "Cancel"}
          </Link>
        )}
        {at < STEPS.length - 1 ? (
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canNext}
            onClick={() => setAt(at + 1)}
          >
            {at === 3 ? "I've given it the code" : "Next"}
          </button>
        ) : null}
      </div>
    </div>
  );
}
