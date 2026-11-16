//! Cache of read credentials per content version, honouring `cache_until`
//! (control API §3: reuse a SAS until its expiry minus 5 minutes).

use std::collections::HashMap;
use std::time::SystemTime;

use crate::types::Credential;
use crate::util::parse_time;

struct Entry {
    cred: Credential,
    /// `None`: never expires (an S3 pointer, which is not a secret).
    until: Option<SystemTime>,
    inserted: SystemTime,
}

pub struct CredentialCache {
    map: HashMap<(u64, u64), Entry>,
    cap: usize,
}

impl CredentialCache {
    pub fn new(cap: usize) -> Self {
        Self {
            map: HashMap::new(),
            cap: cap.max(1),
        }
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    /// The cached credential for `(file_id, version_id)` if `now` is before
    /// its `cache_until`.
    pub fn get(&mut self, file_id: u64, version_id: u64, now: SystemTime) -> Option<Credential> {
        let key = (file_id, version_id);
        let e = self.map.get(&key)?;
        if e.until.is_some_and(|u| now >= u) {
            self.map.remove(&key);
            return None;
        }
        Some(e.cred.clone())
    }

    /// Cache `cred`. A credential whose `cache_until` is missing or
    /// unparsable, or already past, is not cached.
    pub fn insert(&mut self, file_id: u64, version_id: u64, cred: Credential, now: SystemTime) {
        // An unknown mode is never cached: a service upgrade may fix it.
        if cred.is_unsupported() {
            return;
        }
        let until = match cred.cache_until() {
            Some(s) => match parse_time(s) {
                Some(t) if t > now => Some(t),
                _ => return,
            },
            None => None,
        };
        if self.map.len() >= self.cap && !self.map.contains_key(&(file_id, version_id)) {
            self.evict(now);
        }
        self.map.insert(
            (file_id, version_id),
            Entry {
                cred,
                until,
                inserted: now,
            },
        );
    }

    pub fn remove(&mut self, file_id: u64, version_id: u64) {
        self.map.remove(&(file_id, version_id));
    }

    /// Drop expired entries; if still full, drop the oldest tenth.
    fn evict(&mut self, now: SystemTime) {
        self.map.retain(|_, e| e.until.is_none_or(|u| now < u));
        if self.map.len() < self.cap {
            return;
        }
        let mut by_age: Vec<_> = self.map.iter().map(|(k, e)| (e.inserted, *k)).collect();
        by_age.sort_unstable();
        for (_, k) in by_age.into_iter().take((self.cap / 10).max(1)) {
            self.map.remove(&k);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::util::format_time;
    use std::time::Duration;

    fn sas(until: SystemTime) -> Credential {
        Credential::AzureUserDelegationSas {
            url: "https://a/c/k?sig=x".into(),
            expires_at: format_time(until + Duration::from_secs(300)),
            cache_until: format_time(until),
        }
    }

    #[test]
    fn honours_cache_until() {
        let t0 = SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        let mut c = CredentialCache::new(10);
        c.insert(1, 2, sas(t0 + Duration::from_secs(3300)), t0);
        assert!(c.get(1, 2, t0 + Duration::from_secs(3299)).is_some());
        assert!(c.get(1, 3, t0).is_none(), "keyed by content version");
        assert!(c.get(1, 2, t0 + Duration::from_secs(3300)).is_none());
        assert!(c.is_empty(), "expired entry dropped");
    }

    #[test]
    fn already_expired_is_not_cached() {
        let t0 = SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        let mut c = CredentialCache::new(10);
        c.insert(1, 2, sas(t0 - Duration::from_secs(1)), t0);
        assert!(c.is_empty());
    }

    #[test]
    fn unsupported_is_never_cached() {
        let t0 = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        let mut c = CredentialCache::new(10);
        c.insert(1, 2, Credential::Unsupported, t0);
        assert!(c.get(1, 2, t0).is_none());
    }

    #[test]
    fn s3_pointer_never_expires_and_cap_evicts() {
        let t0 = SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        let mut c = CredentialCache::new(3);
        let s3 = Credential::S3RolesAnywhere {
            bucket: "b".into(),
            key: "k".into(),
            version_id: None,
            etag: Some("\"e\"".into()),
            crc64nvme: None,
            region: "eu-west-1".into(),
            endpoint: "e".into(),
            addressing: "virtual_host".into(),
            tls_max_version: Some("1.2".into()),
            requester_pays: false,
            private_network: true,
        };
        c.insert(9, 9, s3, t0);
        for i in 0..5 {
            c.insert(
                i,
                0,
                sas(t0 + Duration::from_secs(60)),
                t0 + Duration::from_secs(i + 1),
            );
        }
        assert!(c.len() <= 3);
        assert!(c.get(4, 0, t0).is_some(), "newest kept");
    }
}
