//! A small bounded TTL cache shared by the id-map and the SMB session lookups.

use std::collections::HashMap;
use std::hash::Hash;
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub struct TtlCache<K, V> {
    ttl: Duration,
    cap: usize,
    map: Mutex<HashMap<K, (Instant, V)>>,
}

impl<K: Eq + Hash + Clone, V: Clone> TtlCache<K, V> {
    pub fn new(ttl: Duration, cap: usize) -> Self {
        TtlCache {
            ttl,
            cap: cap.max(1),
            map: Mutex::new(HashMap::new()),
        }
    }

    pub fn get(&self, k: &K) -> Option<V> {
        let m = self.map.lock().unwrap_or_else(|e| e.into_inner());
        match m.get(k) {
            Some((at, v)) if at.elapsed() < self.ttl => Some(v.clone()),
            _ => None,
        }
    }

    pub fn put(&self, k: K, v: V) {
        let mut m = self.map.lock().unwrap_or_else(|e| e.into_inner());
        if m.len() >= self.cap {
            let ttl = self.ttl;
            m.retain(|_, (at, _)| at.elapsed() < ttl);
            if m.len() >= self.cap {
                m.clear();
            }
        }
        m.insert(k, (Instant::now(), v));
    }

    pub fn remove(&self, k: &K) {
        self.map.lock().unwrap_or_else(|e| e.into_inner()).remove(k);
    }

    pub fn clear(&self) {
        self.map.lock().unwrap_or_else(|e| e.into_inner()).clear();
    }

    pub fn len(&self) -> usize {
        self.map.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expires_and_bounds() {
        let c = TtlCache::new(Duration::from_millis(30), 2);
        c.put(1, "a");
        assert_eq!(c.get(&1), Some("a"));
        std::thread::sleep(Duration::from_millis(40));
        assert_eq!(c.get(&1), None);
        c.put(2, "b");
        c.put(3, "c");
        c.put(4, "d");
        assert!(c.len() <= 2);
        assert_eq!(c.get(&4), Some("d"));
    }
}
