import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("settings", () => {
  it("is in the nav for an ArchiveAdmin and opens on Storage", async () => {
    const { container } = renderApp("/settings", { persona: "admin" });
    expect(await screen.findByRole("heading", { name: "Storage" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: /Settings/ })).toBeInTheDocument();
    expect(screen.getByText("Private networking is on")).toBeInTheDocument();
    expect(await screen.findByRole("article", { name: "azure-weu" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("tells anyone else that settings are for ArchiveAdmins", async () => {
    renderApp("/settings/storage", { persona: "steward" });
    expect(await screen.findByText("Settings are for ArchiveAdmins.")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).queryByRole("link", { name: /Settings/ })).not.toBeInTheDocument();
    expect(requestLog.some((r) => r.path.startsWith("/v1/settings"))).toBe(false);
  });

  it("runs a backend's live checks on request", async () => {
    const { user } = renderApp("/settings/storage");
    await user.click(await screen.findByRole("button", { name: "Run checks for azure-worm" }));
    const list = await screen.findByRole("list", { name: "Checks for azure-worm" });
    expect(within(list).getByText("Private endpoint DNS")).toBeInTheDocument();
    expect(within(list).getByText(/Assign Storage Blob Data Contributor/)).toBeInTheDocument();
  });

  it("adds an Azure backend, and shows the server's refusal inline", async () => {
    const { user } = renderApp("/settings/storage");
    await user.click(await screen.findByRole("button", { name: "Add backend" }));
    const dialog = screen.getByRole("dialog", { name: "Add a storage backend" });
    expect(within(dialog).queryByText(/public/i)).not.toBeInTheDocument();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Live storage account" }),
      "omnilive02",
    );
    await user.click(within(dialog).getByRole("button", { name: "Add backend" }));
    expect(
      await within(dialog).findByText(/archive: the archive account is required/),
    ).toBeInTheDocument();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Archive storage account" }),
      "omniarch02",
    );
    await user.click(within(dialog).getByRole("button", { name: "Add backend" }));
    await waitFor(() =>
      expect(
        requestLog.filter((r) => r.method === "POST" && r.path === "/v1/settings/storage"),
      ).toHaveLength(2),
    );
    const sent = requestLog.filter((r) => r.path === "/v1/settings/storage" && r.method === "POST");
    expect(sent[1]?.body).toMatchObject({
      kind: "azure",
      live: "omnilive02",
      archive: "omniarch02",
    });
    expect(sent[1]?.headers["idempotency-key"]).toBeTruthy();
  });

  it("shows the trust policy for a pasted role and tests it", async () => {
    const { user, container } = renderApp("/settings/aws");
    expect(await screen.findByText("No AWS keys are stored anywhere")).toBeInTheDocument();
    await user.type(
      screen.getByRole("textbox", { name: "Role ARN" }),
      "arn:aws:iam::123456789012:role/omnifile-service",
    );
    await user.click(screen.getByRole("button", { name: "Show trust policy" }));
    const pre = await screen.findByLabelText("Trust policy JSON");
    expect(pre.textContent).toContain("sts:AssumeRoleWithWebIdentity");
    expect(pre.textContent).toContain("oidc-provider/sts.windows.net/");
    await user.click(screen.getByRole("button", { name: "Test" }));
    expect(await screen.findByText("Test passed", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText(/assumed-role\/omnifile-service/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows identity, consent and who am I", async () => {
    const { container } = renderApp("/settings/identity");
    expect(
      await screen.findByRole("link", { name: "Open the admin consent page" }),
    ).toHaveAttribute("href", expect.stringContaining("/adminconsent?client_id="));
    expect(screen.getByText(/Only a tenant administrator can grant consent/)).toBeInTheDocument();
    const roles = screen.getByRole("table", { name: "App roles" });
    expect(within(roles).getByRole("row", { name: /ArchiveAdmin/ })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Who am I" })).toBeInTheDocument();
    expect(screen.getByText("avery.chen@contoso-energy.com")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("sends a test card and shows it arrived", async () => {
    const { user, container } = renderApp("/settings/teams");
    expect(await screen.findByText("Cards are reaching people.")).toBeInTheDocument();
    expect(screen.getByText(/deploy\/teams\/build.sh/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send me a test card" }));
    expect(await screen.findByText("Test passed", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText("Card sent to your Teams chat.")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("sends a test email", async () => {
    const { user } = renderApp("/settings/email");
    expect(await screen.findByText("omnifile-notify@contoso-energy.com")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send me a test email" }));
    expect(await screen.findByText("Test passed", {}, { timeout: 4000 })).toBeInTheDocument();
  });

  it("shows Storage Mover read-only", async () => {
    const { container } = renderApp("/settings/storage-mover");
    const projects = await screen.findByRole("table", { name: "Projects" });
    expect(within(projects).getByText("In use")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Endpoints" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows prices and alerts", async () => {
    const { container } = renderApp("/settings/prices-alerts");
    expect(await screen.findByRole("table", { name: "Azure Blob rates" })).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Amazon S3 rates" })).toBeInTheDocument();
    const alerts = await screen.findByRole("table", { name: "Alerts" });
    expect(within(alerts).getByText("Firing")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });
});
