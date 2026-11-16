//! Private networking (decision 22): when the setup wizard selects it, the
//! edge reaches storage, and the service, only over private addresses, and
//! never falls back to a public path.
//!
//! The policy is process-wide: the daemon sets it once at startup from
//! `edge.toml` (`[network] private_only`). Every HTTP client built by
//! [`crate::tls::http_client`] then resolves names through [`PrivateResolver`],
//! which drops every public address. A name with only public addresses fails
//! to resolve, with an error that says why. A URL whose host is a literal IP
//! skips DNS, so [`check_host`] covers that case where URLs are parsed.
//!
//! "Private" means RFC 1918, shared address space (RFC 6598, which some
//! private links use), and IPv6 unique-local (fc00::/7). Link-local
//! addresses are not storage endpoints and count as public. Loopback is
//! refused too, unless the sim's test switch allows it ([`set_loopback_ok`]):
//! a storage URL from the service naming `127.0.0.1` would otherwise reach the
//! appliance's own local services (M7 review).

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::atomic::{AtomicBool, Ordering};

static PRIVATE_ONLY: AtomicBool = AtomicBool::new(false);
static LOOPBACK_OK: AtomicBool = AtomicBool::new(false);

/// Lets private networking reach loopback addresses: the sim and the tests
/// only (the daemon sets it with `OMNIFILE_BLOB_ALLOW_HTTP=1`).
pub fn set_loopback_ok(on: bool) {
    LOOPBACK_OK.store(on, Ordering::SeqCst);
}

/// [`is_private`], plus loopback when [`set_loopback_ok`] allows it.
pub fn allowed(ip: IpAddr) -> bool {
    let loopback = ip.is_loopback()
        || matches!(ip, IpAddr::V6(v6) if v6.to_ipv4_mapped().is_some_and(|v4| v4.is_loopback()));
    is_private(ip) || (loopback && LOOPBACK_OK.load(Ordering::SeqCst))
}

/// Turns private-only networking on or off for every client built afterwards
/// (and every URL checked afterwards). Call once, before building clients.
pub fn set_private_only(on: bool) {
    PRIVATE_ONLY.store(on, Ordering::SeqCst);
}

pub fn private_only() -> bool {
    PRIVATE_ONLY.load(Ordering::SeqCst)
}

/// Whether `ip` is an address private networking may use.
pub fn is_private(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => is_private_v4(v4),
        IpAddr::V6(v6) => {
            if let Some(v4) = v6.to_ipv4_mapped() {
                return is_private_v4(v4);
            }
            (v6.segments()[0] & 0xfe00) == 0xfc00
        }
    }
}

fn is_private_v4(v4: Ipv4Addr) -> bool {
    let o = v4.octets();
    v4.is_private() || (o[0] == 100 && (o[1] & 0xc0) == 64)
}

/// The addresses of `addrs` private networking may use, in their order.
pub fn keep_private(addrs: impl IntoIterator<Item = SocketAddr>) -> Vec<SocketAddr> {
    addrs.into_iter().filter(|a| allowed(a.ip())).collect()
}

/// Refuses a URL host that is a public IP literal while private networking
/// is on. Names are left to the resolver.
pub fn check_host(host: Option<&str>) -> Result<(), String> {
    if !private_only() {
        return Ok(());
    }
    // A URL's IPv6 literal comes bracketed ("[fd00::1]").
    let Some(Ok(ip)) = host.map(|h| {
        h.trim_start_matches('[')
            .trim_end_matches(']')
            .parse::<IpAddr>()
    }) else {
        return Ok(());
    };
    if allowed(ip) {
        Ok(())
    } else {
        Err(format!(
            "private networking is on, and {ip} is a public address: \
             storage is reached only through private endpoints"
        ))
    }
}

/// A DNS resolver that returns only private addresses (reqwest's
/// `dns_resolver`). Used only while private networking is on.
#[derive(Debug, Default)]
pub struct PrivateResolver;

impl reqwest::dns::Resolve for PrivateResolver {
    fn resolve(&self, name: reqwest::dns::Name) -> reqwest::dns::Resolving {
        let host = name.as_str().to_string();
        Box::pin(async move {
            let all: Vec<SocketAddr> = tokio::net::lookup_host((host.as_str(), 0)).await?.collect();
            let kept = keep_private(all.iter().copied());
            if kept.is_empty() {
                let shown: Vec<String> = all.iter().map(|a| a.ip().to_string()).collect();
                return Err(format!(
                    "{host} resolves only to public addresses ({}): private networking is on, \
                     so the edge reaches it only through a private endpoint. Check the \
                     private endpoint and its DNS zone (privatelink.*) on this network",
                    if shown.is_empty() {
                        "none".into()
                    } else {
                        shown.join(", ")
                    }
                )
                .into());
            }
            let addrs: reqwest::dns::Addrs = Box::new(kept.into_iter());
            Ok(addrs)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(s: &str) -> IpAddr {
        s.parse().unwrap()
    }

    #[test]
    fn loopback_only_with_the_sim_switch() {
        assert!(!allowed(ip("127.0.0.1")) && !allowed(ip("::1")));
        set_loopback_ok(true);
        assert!(allowed(ip("127.0.0.1")) && allowed(ip("::ffff:127.0.0.1")));
        assert!(!allowed(ip("8.8.8.8")));
        set_loopback_ok(false);
        assert!(!allowed(ip("127.0.0.1")));
    }

    #[test]
    fn private_ranges() {
        for p in [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.1.1",
            "100.64.0.1",
            "100.127.255.255",
            "fd12:3456::1",
            "fc00::1",
            "::ffff:10.1.2.3",
        ] {
            assert!(is_private(ip(p)), "{p} is private");
        }
        for p in [
            "20.60.1.2",
            "52.239.1.1",
            "172.32.0.1",
            "100.128.0.1",
            "169.254.169.254",
            "8.8.8.8",
            "2603:1000::1",
            "fe80::1",
            "::ffff:20.60.1.2",
            // Loopback is not a storage endpoint (M7 review).
            "127.0.0.1",
            "::1",
            "::ffff:127.0.0.1",
        ] {
            assert!(!is_private(ip(p)), "{p} is public");
        }
    }

    #[test]
    fn keeps_only_private_in_order() {
        let a: Vec<SocketAddr> = [
            "20.60.1.2:443",
            "10.1.0.4:443",
            "[fd00::4]:443",
            "52.1.1.1:443",
        ]
        .iter()
        .map(|s| s.parse().unwrap())
        .collect();
        let kept = keep_private(a);
        assert_eq!(kept.len(), 2);
        assert_eq!(kept[0].ip(), ip("10.1.0.4"));
        assert_eq!(kept[1].ip(), ip("fd00::4"));
    }
}
