import { screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SID } from "../../mocks/db";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

async function openDialog(user: UserEvent) {
  await user.click(await screen.findByRole("button", { name: "Add share" }));
  return screen.findByRole("dialog", { name: "Add a share" });
}

async function pickGroup(user: UserEvent, dialog: HTMLElement, role: string, search: string) {
  await user.type(within(dialog).getByLabelText(`${role} group`), search);
  const results = await within(dialog).findByRole("list", { name: `Groups matching ${search}` });
  await user.click(await within(results).findByRole("button", { name: new RegExp(search, "i") }));
}

/** Name, a new dataset on edge-houston-1 in azure-weu, and the Owners group. */
async function fillBasics(user: UserEvent, dialog: HTMLElement, name: string) {
  await user.type(within(dialog).getByLabelText("Share name"), name);
  await within(dialog).findByRole("option", { name: "edge-houston-1" });
  await user.selectOptions(within(dialog).getByLabelText("Which edge serves it?"), "1");
  await within(dialog).findByRole("option", { name: /^azure-weu \(/ });
  await user.selectOptions(within(dialog).getByLabelText("Where is it stored?"), "1");
  await pickGroup(user, dialog, "Owners", "GG-Seismic-Owners");
}

const posts = (path: string) => requestLog.filter((r) => r.method === "POST" && r.path === path);

describe("add a share", () => {
  it("is offered to ArchiveAdmin only", async () => {
    renderApp("/shares", { persona: "admin" });
    expect(await screen.findByRole("button", { name: "Add share" })).toBeInTheDocument();
  });

  it("is not offered to a DataSteward who owns a share", async () => {
    renderApp("/shares", { persona: "steward" });
    await screen.findByRole("table", { name: "Shares" });
    expect(screen.queryByRole("button", { name: "Add share" })).not.toBeInTheDocument();
  });

  it("shows the Kerberos throughput note whenever Kerberos is chosen", async () => {
    const { user, container } = renderApp("/shares", { persona: "admin" });
    const dialog = await openDialog(user);
    // NFSv4 with Kerberos (krb5p) is the default.
    expect(within(dialog).getByRole("radio", { name: /krb5p/ })).toBeChecked();
    expect(within(dialog).getByText("Kerberos and throughput")).toBeInTheDocument();
    expect(
      within(dialog).getByText(/53% fewer IOPS with krb5, and about 77% fewer with krb5p/),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("radio", { name: /krb5i/ }));
    expect(within(dialog).getByText("Kerberos and throughput")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("radio", { name: /Trust named hosts/ }));
    expect(within(dialog).queryByText("Kerberos and throughput")).not.toBeInTheDocument();
    // SMB only: no NFS questions at all.
    await user.click(within(dialog).getByRole("checkbox", { name: /NFSv4/ }));
    expect(
      within(dialog).queryByText("How do NFS clients prove who they are?"),
    ).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("needs named hosts, without wildcards, for sec=sys", async () => {
    const { user } = renderApp("/shares", { persona: "admin" });
    const dialog = await openDialog(user);
    await fillBasics(user, dialog, "hpc-scratch");
    const create = within(dialog).getByRole("button", { name: "Create share" });
    expect(create).toBeEnabled();
    await user.click(within(dialog).getByRole("radio", { name: /Trust named hosts/ }));
    expect(create).toBeDisabled();
    expect(within(dialog).getByText(/List at least one host or subnet/)).toBeInTheDocument();
    const hosts = within(dialog).getByLabelText("Trusted hosts and subnets");
    await user.type(hosts, "hpc-*.contoso-energy.com");
    expect(within(dialog).getByText(/lets in any host/)).toBeInTheDocument();
    expect(create).toBeDisabled();
    await user.clear(hosts);
    await user.type(hosts, "10.20.0.0/16{Enter}hpc-login1.contoso-energy.com");
    expect(create).toBeEnabled();
    await user.click(create);
    await within(dialog).findByText("hpc-scratch is ready");
    expect(posts("/v1/shares")[0]?.body).toMatchObject({
      protocols: ["smb", "nfs4"],
      nfs_sec: ["sys"],
      nfs_clients: ["10.20.0.0/16", "hpc-login1.contoso-energy.com"],
      root_squash: true,
    });
    expect(within(dialog).getByText(/sec=sys: the NFS client says who the user is/)).toBeVisible();
  });

  it("creates the dataset, then the share, and links to its settings", async () => {
    const { user, db } = renderApp("/shares", { persona: "admin" });
    const dialog = await openDialog(user);
    await fillBasics(user, dialog, "gravity-2026");
    await pickGroup(user, dialog, "Readers", "GG-Data-Readers");
    await user.click(within(dialog).getByRole("radio", { name: /Wait only if it's quick/ }));
    const wait = within(dialog).getByLabelText("Wait at most");
    await user.clear(wait);
    await user.type(wait, "10");
    await user.click(within(dialog).getByRole("button", { name: "Create share" }));
    expect(await within(dialog).findByText("gravity-2026 is ready")).toBeInTheDocument();

    expect(posts("/v1/datasets").map((r) => r.body)).toEqual([
      {
        name: "gravity-2026",
        backend_id: "1",
        edge_id: "1",
        storage_layout: "two_account",
        key_prefix: "",
      },
    ]);
    const dataset = db.datasets.find((d) => d.name === "gravity-2026");
    expect(dataset).toBeDefined();
    const shareCalls = posts("/v1/shares");
    expect(shareCalls).toHaveLength(1);
    expect(shareCalls[0]?.body).toEqual({
      dataset_id: dataset?.dataset_id,
      name: "gravity-2026",
      protocols: ["smb", "nfs4"],
      nfs_sec: ["krb5p"],
      owners_group_sid: SID.seismicOwners,
      readers_group_sid: SID.dataReaders,
      on_open: "auto",
      wait_threshold_minutes: 10,
      smb_encrypt: false,
      rewind_depth_days: 0,
      rewind_depth_max_days: 0,
    });
    // Each POST had its own Idempotency-Key and the CSRF token.
    const keys = [...posts("/v1/datasets"), ...shareCalls].map((r) => r.headers["idempotency-key"]);
    expect(new Set(keys).size).toBe(2);

    // The share's checks, as callouts, and its settings link.
    expect(within(dialog).getByText(/This share uses Kerberos \(krb5p\)/)).toBeInTheDocument();
    const share = db.shares.find((s) => s.name === "gravity-2026");
    expect(within(dialog).getByRole("link", { name: /gravity-2026's settings/ })).toHaveAttribute(
      "href",
      `/shares/${share?.share_id}`,
    );
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog", { name: "Add a share" })).not.toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Shares" });
    expect(await within(table).findByRole("link", { name: "gravity-2026" })).toBeInTheDocument();
  });

  it("keeps a dataset whose share failed, and reuses it on retry", async () => {
    const { user, db } = renderApp("/shares", { persona: "admin" });
    const dialog = await openDialog(user);
    // long-term-storage is taken: the dataset is made, the share refused.
    await fillBasics(user, dialog, "long-term-storage");
    await user.click(within(dialog).getByRole("button", { name: "Create share" }));
    expect(await within(dialog).findByText("The share wasn't created")).toBeInTheDocument();
    expect(within(dialog).getByText(/already has a share named long-term-storage/)).toBeVisible();
    const dataset = db.datasets.find((d) => d.name === "long-term-storage");
    expect(dataset).toBeDefined();
    expect(within(dialog).getByText(/was created and is kept/)).toBeInTheDocument();

    // The form now points at that dataset.
    expect(
      within(dialog).getByRole("radio", { name: /In a dataset that has no share yet/ }),
    ).toBeChecked();
    expect(within(dialog).getByLabelText("Dataset")).toHaveValue(dataset?.dataset_id);

    const name = within(dialog).getByLabelText("Share name");
    await user.clear(name);
    await user.type(name, "long-term-storage-2");
    await user.click(within(dialog).getByRole("button", { name: "Create share" }));
    expect(await within(dialog).findByText("long-term-storage-2 is ready")).toBeInTheDocument();
    expect(posts("/v1/datasets")).toHaveLength(1);
    const shareCalls = posts("/v1/shares");
    expect(shareCalls).toHaveLength(2);
    expect(shareCalls.map((r) => (r.body as { dataset_id: string }).dataset_id)).toEqual([
      dataset?.dataset_id,
      dataset?.dataset_id,
    ]);
  });

  it("offers an existing dataset that is on an edge and has no share", async () => {
    const { user } = renderApp("/shares", { persona: "admin" });
    const dialog = await openDialog(user);
    await user.type(within(dialog).getByLabelText("Share name"), "gravity");
    await user.click(
      await within(dialog).findByRole("radio", { name: /In a dataset that has no share yet/ }),
    );
    const select = within(dialog).getByLabelText("Dataset");
    const options = within(select)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toEqual([
      "Choose a dataset",
      "gravity-surveys (Azure Blob, on edge-houston-1)",
    ]);
    await user.selectOptions(select, "4");
    await pickGroup(user, dialog, "Owners", "GG-Seismic-Owners");
    await user.click(within(dialog).getByRole("button", { name: "Create share" }));
    await within(dialog).findByText("gravity is ready");
    expect(posts("/v1/datasets")).toHaveLength(0);
    expect(posts("/v1/shares")[0]?.body).toMatchObject({ dataset_id: "4", name: "gravity" });
  });

  it("shares a folder of a shared dataset, sending its path", async () => {
    const { user, db, container } = renderApp("/shares", { persona: "admin" });
    const dialog = await openDialog(user);
    await user.type(within(dialog).getByLabelText("Share name"), "seismic-dataset");
    await user.click(
      await within(dialog).findByRole("radio", { name: /A folder in a shared dataset/ }),
    );
    expect(within(dialog).getByText(/The folder\s+must already exist/)).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Sharing a folder gives it its own groups and policy/),
    ).toBeInTheDocument();
    const select = within(dialog).getByLabelText("Shared dataset");
    // Only datasets on an edge that already have a share are offered.
    const options = within(select)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toContain(
      "Seismic survey archive (shared as long-term-storage, processing-scratch, on edge-houston-1)",
    );
    expect(options.some((o) => o.startsWith("gravity-surveys"))).toBe(false);
    await user.selectOptions(select, "1");
    const folder = within(dialog).getByLabelText("Folder");
    await user.type(folder, "dataset");
    expect(within(dialog).getByText(/Start at the dataset's root, with \//)).toBeInTheDocument();
    expect(folder).toHaveAttribute("aria-invalid", "true");
    await user.clear(folder);
    await user.type(folder, "/dataset");
    await pickGroup(user, dialog, "Owners", "GG-Seismic-Owners");
    await expectNoAxeViolations(container);
    await user.click(within(dialog).getByRole("button", { name: "Create share" }));
    expect(await within(dialog).findByText("seismic-dataset is ready")).toBeInTheDocument();
    expect(posts("/v1/datasets")).toHaveLength(0);
    expect(posts("/v1/shares")[0]?.body).toMatchObject({
      dataset_id: "1",
      name: "seismic-dataset",
      path: "/dataset",
    });
    expect(within(dialog).getByText("/dataset")).toBeInTheDocument();
    expect(db.shares.find((s) => s.name === "seismic-dataset")?.root_dir_id).toBe("101");
  });

  it("shows the server's 'share the root first' refusal on the folder field", async () => {
    const { user, db } = renderApp("/shares", { persona: "admin" });
    // Neither of the dataset's shares serves its root.
    db.sharePaths.set("1", "/dataset");
    db.sharePaths.set("2", "/reports");
    const dialog = await openDialog(user);
    await user.type(within(dialog).getByLabelText("Share name"), "seismic-reports");
    await user.click(
      await within(dialog).findByRole("radio", { name: /A folder in a shared dataset/ }),
    );
    await user.selectOptions(within(dialog).getByLabelText("Shared dataset"), "1");
    const folder = within(dialog).getByLabelText("Folder");
    await user.type(folder, "/reports/2026");
    await pickGroup(user, dialog, "Owners", "GG-Seismic-Owners");
    await user.click(within(dialog).getByRole("button", { name: "Create share" }));
    expect(await within(dialog).findByText("The share wasn't created")).toBeInTheDocument();
    expect(posts("/v1/shares")[0]?.body).toMatchObject({ path: "/reports/2026" });
    expect(folder).toHaveAttribute("aria-invalid", "true");
    expect(folder).toHaveAccessibleDescription(/share the dataset's root first/);
    // Editing the folder clears the server's message.
    await user.type(folder, "x");
    expect(folder).not.toHaveAccessibleDescription(/share the dataset's root first/);
  });

  it("never offers S3 as a dataset's home", async () => {
    const { user } = renderApp("/shares", { persona: "admin" });
    const dialog = await openDialog(user);
    await within(dialog).findByRole("option", { name: /^azure-weu \(/ });
    const storage = within(dialog).getByLabelText("Where is it stored?");
    const names = within(storage)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(names).toEqual([
      "Choose storage",
      "azure-weu (Azure Blob, private endpoint)",
      "azure-weu-worm (Azure Blob, private endpoint)",
    ]);
    expect(names.some((n) => n.includes("aws-us-east-1"))).toBe(false);
    expect(within(dialog).getByText(/Amazon S3 isn't offered/)).toBeInTheDocument();
  });

  it("refuses a reserved or malformed name before sending anything", async () => {
    const { user } = renderApp("/shares", { persona: "admin" });
    const dialog = await openDialog(user);
    const name = within(dialog).getByLabelText("Share name");
    await user.type(name, "homes");
    expect(within(dialog).getByText(/homes is reserved by Samba/)).toBeInTheDocument();
    expect(name).toHaveAttribute("aria-invalid", "true");
    await user.clear(name);
    await user.type(name, "-bad");
    expect(within(dialog).getByText(/starting with a letter or digit/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Create share" })).toBeDisabled();
    await waitFor(() => expect(posts("/v1/shares")).toHaveLength(0));
  });
});
