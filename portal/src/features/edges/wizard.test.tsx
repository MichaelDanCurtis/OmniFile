import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("the Add edge wizard", () => {
  it("goes from a platform to the new edge's first heartbeat", async () => {
    const { container, user } = renderApp("/edges/new", { persona: "admin" });
    const steps = await screen.findByRole("list", { name: "Add an edge" });
    expect(within(steps).getAllByRole("listitem")).toHaveLength(5);
    const next = screen.getByRole("button", { name: "Next" });
    expect(next).toBeDisabled();
    // AWS has no image yet: shown, locked, and says why.
    expect(await screen.findByRole("radio", { name: /AWS EC2/ })).toBeDisabled();
    expect(screen.getByText(/No AWS image is built yet/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
    await user.click(screen.getByRole("radio", { name: /KVM or Proxmox/ }));
    await user.click(next);

    // The image: the release's file name, and where it is; no invented link.
    expect(await screen.findByText("omnifile-edge_0.3.0.qcow2")).toBeInTheDocument();
    expect(screen.getByText("Not published for download")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Download/ })).not.toBeInTheDocument();
    expect(screen.getByText(/virt-install --name/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
    await user.click(screen.getByRole("button", { name: "Next" }));

    // Sizing and network.
    expect(await screen.findByText("8 GiB, fixed (not dynamic)")).toBeInTheDocument();
    expect(screen.getByText(/SMB \(TCP 445\), NFS \(TCP 2049\)/)).toBeInTheDocument();
    expect(screen.getByText(/ExpressRoute or a site-to-site VPN/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));

    // Enrol: a bad name can't be sent; a taken one says so; then the code, once.
    const name = await screen.findByLabelText("Name");
    const create = screen.getByRole("button", { name: "Create code" });
    expect(screen.getByRole("button", { name: "I've given it the code" })).toBeDisabled();
    await user.type(name, "Bad_Name");
    expect(create).toBeDisabled();
    await user.clear(name);
    await user.type(name, "edge-houston-1");
    await user.click(create);
    expect(await screen.findByText(/an edge named edge-houston-1 exists/)).toBeInTheDocument();
    await user.clear(name);
    await user.type(name, "edge-dallas-1");
    await user.click(screen.getByRole("button", { name: "Create code" }));
    const code = await screen.findByLabelText("Enrolment code for edge-dallas-1");
    expect((code as HTMLTextAreaElement).value).toMatch(/^ofe1\./);
    expect(screen.getByText(/shown only once/)).toBeInTheDocument();
    expect(screen.getByText(/\d+ h \d+ min left/)).toBeInTheDocument();
    expect(screen.getByText("sudo omnifile-setup enrol")).toBeInTheDocument();
    // KVM gets the unattended form too, with this code in it.
    expect(screen.getByText(/omnifile\.enrol-bundle=/)).toBeInTheDocument();
    expect(requestLog.filter((r) => r.method === "POST" && r.path === "/v1/edges")).toHaveLength(2);
    await expectNoAxeViolations(container);
    await user.click(screen.getByRole("button", { name: "I've given it the code" }));

    // Waiting, then green when the edge enrols and its heartbeat arrives.
    expect(
      await screen.findByRole("heading", { name: "Waiting for edge-dallas-1 to enrol" }),
    ).toBeInTheDocument();
    const wait = screen.getByRole("list", { name: "Enrolment" });
    expect(within(wait).getByText(/Waiting for omnifile-setup/)).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "edge-dallas-1 is online" }, { timeout: 4000 }),
    ).toBeInTheDocument();
    expect(within(wait).getByText(/^Heard /)).toBeInTheDocument();
    expect(within(wait).getAllByText(/: done/)).toHaveLength(3);
    await expectNoAxeViolations(container);
  });

  it("shows each hypervisor's own import steps", async () => {
    const { user } = renderApp("/edges/new", { persona: "admin" });
    await user.click(await screen.findByRole("radio", { name: /Hyper-V/ }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("omnifile-edge_0.3.0.vhdx")).toBeInTheDocument();
    expect(screen.getByText(/Enable-VMTPM -VMName \$Name/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("radio", { name: /VMware vSphere/ }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("omnifile-edge_0.3.0.ova")).toBeInTheDocument();
    expect(screen.getAllByText(/govc import\.ova/).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("radio", { name: /Azure VM/ }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("omnifile-edge_0.3.0.vhd")).toBeInTheDocument();
    expect(screen.getByText("Not yet tried on a real subscription")).toBeInTheDocument();
  });

  it("follows the customer guide's step 53 as written", async () => {
    // docs/guide/README.md step 53: open /edges, click Add edge, click Skip to the code,
    // fill Name, click Create code, expect Copy this code now, click Copy code.
    const { user } = renderApp("/edges", { persona: "admin" });
    await user.click(await screen.findByRole("link", { name: "Add edge" }));
    await user.click(await screen.findByRole("button", { name: "Skip to the code" }));
    await user.type(await screen.findByLabelText("Name"), "edge-dallas-1");
    await user.click(screen.getByRole("button", { name: "Create code" }));
    expect(await screen.findByText(/Copy this code now/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
    // Skipped, Back returns to the platforms, not to a step with none chosen.
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("radio", { name: /Hyper-V/ })).toBeInTheDocument();
  });

  it("is for ArchiveAdmins only", async () => {
    renderApp("/edges/new", { persona: "steward" });
    expect(await screen.findByText("Adding edges is for ArchiveAdmin.")).toBeInTheDocument();
    await waitFor(() =>
      expect(requestLog.some((r) => r.path === "/v1/edges/appliance")).toBe(false),
    );
  });
});
