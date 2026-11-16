import { describe, expect, it } from "vitest";
import { notPrivateWhy } from "./privateWhy";

describe("why an account isn't private", () => {
  it("names the private endpoint first, then the DNS zone, then public access", () => {
    const base = {
      blob_private_endpoint: true,
      dns_linked: true,
      public_network_access: "Enabled",
    };
    expect(notPrivateWhy({ ...base, blob_private_endpoint: false })).toMatch(/private endpoint/);
    expect(notPrivateWhy({ ...base, dns_linked: false })).toMatch(/privatelink\.blob DNS zone/);
    expect(notPrivateWhy(base)).toMatch(/public network access off/);
  });
});
