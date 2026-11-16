/**
 * Why a discovered storage account isn't reachable privately (decision 22), as what to
 * fix first. The service calls an account private only with an approved blob private
 * endpoint, a privatelink.blob DNS zone linked to its VNet, and public access closed.
 */
export function notPrivateWhy(a: {
  blob_private_endpoint: boolean;
  dns_linked: boolean;
  public_network_access: string | null;
}): string {
  if (!a.blob_private_endpoint) {
    return "Give it an approved blob private endpoint and turn public access off first.";
  }
  if (!a.dns_linked) {
    return "Link a privatelink.blob DNS zone to the service's VNet first.";
  }
  return "Turn public network access off (or deny by default with no IP or VNet rule) first.";
}
