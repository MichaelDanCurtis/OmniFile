//! A rate cap for background header prefetches: at most `per_sec` starts a
//! second, with no burst beyond one.

use std::time::{Duration, Instant};

pub struct RateCap {
    interval: Option<Duration>,
    next: Instant,
}

impl RateCap {
    /// `per_sec <= 0`: no prefetching at all ([`RateCap::wait`] never returns
    /// a slot; check [`RateCap::enabled`]).
    pub fn new(per_sec: f64) -> Self {
        let interval =
            (per_sec > 0.0 && per_sec.is_finite()).then(|| Duration::from_secs_f64(1.0 / per_sec));
        Self {
            interval,
            next: Instant::now(),
        }
    }

    pub fn enabled(&self) -> bool {
        self.interval.is_some()
    }

    /// How long to wait before the next start, and reserve it.
    pub fn reserve(&mut self, now: Instant) -> Option<Duration> {
        let i = self.interval?;
        let at = self.next.max(now);
        self.next = at + i;
        Some(at - now)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spaces_starts() {
        let mut r = RateCap::new(10.0);
        let t = Instant::now();
        assert_eq!(r.reserve(t), Some(Duration::ZERO));
        let d = r.reserve(t).unwrap();
        assert!(
            d >= Duration::from_millis(99) && d <= Duration::from_millis(101),
            "{d:?}"
        );
        assert!(!RateCap::new(0.0).enabled());
        assert_eq!(RateCap::new(0.0).reserve(t), None);
    }
}
