//! Test doubles: an in-memory record transport and a scripted FSAL client.
//! Used by this crate's tests and by the daemon's integration tests; the
//! real transport is [`crate::socket`] on Linux.

use std::io;
use std::time::Duration;

use tokio::sync::{Mutex, mpsc};

use crate::server::PacketIo;
use crate::wire::{self, Frame, Hello, Message, feature};

/// One end of an in-memory record pipe.
pub struct MemIo {
    tx: mpsc::UnboundedSender<Vec<u8>>,
    rx: Mutex<mpsc::UnboundedReceiver<Vec<u8>>>,
}

/// Two connected ends.
pub fn pair() -> (MemIo, MemIo) {
    let (a_tx, b_rx) = mpsc::unbounded_channel();
    let (b_tx, a_rx) = mpsc::unbounded_channel();
    (
        MemIo {
            tx: a_tx,
            rx: Mutex::new(a_rx),
        },
        MemIo {
            tx: b_tx,
            rx: Mutex::new(b_rx),
        },
    )
}

impl PacketIo for MemIo {
    async fn recv(&self) -> io::Result<Option<Vec<u8>>> {
        Ok(self.rx.lock().await.recv().await)
    }
    async fn send(&self, rec: &[u8]) -> io::Result<()> {
        self.tx
            .send(rec.to_vec())
            .map_err(|_| io::Error::from(io::ErrorKind::BrokenPipe))
    }
}

/// A scripted FSAL: sends what the test says, and collects what arrives.
pub struct FakeFsal<T: PacketIo> {
    pub io: T,
    next_req: u64,
}

impl<T: PacketIo> FakeFsal<T> {
    pub fn new(io: T) -> Self {
        FakeFsal { io, next_req: 1 }
    }

    pub async fn send(&mut self, msg: &Message) -> u64 {
        let id = self.next_req;
        self.next_req += 1;
        self.io.send(&wire::encode(msg, id)).await.expect("send");
        id
    }

    pub async fn send_with_id(&mut self, msg: &Message, req_id: u64) {
        self.io
            .send(&wire::encode(msg, req_id))
            .await
            .expect("send");
    }

    pub async fn send_raw(&mut self, rec: &[u8]) {
        self.io.send(rec).await.expect("send");
    }

    /// Next frame, or `None` on EOF or after `timeout`.
    pub async fn recv_timeout(&mut self, timeout: Duration) -> Option<Frame> {
        match tokio::time::timeout(timeout, self.io.recv()).await {
            Ok(Ok(Some(rec))) => Some(wire::decode(&rec).expect("daemon sent a malformed record")),
            _ => None,
        }
    }

    pub async fn recv(&mut self) -> Frame {
        self.recv_timeout(Duration::from_secs(5))
            .await
            .expect("expected a message from the daemon")
    }

    /// `HELLO` → `HELLO_ACK`, then the whole resync. Returns the ack and the
    /// resynced recall states.
    pub async fn handshake(&mut self) -> (wire::HelloAck, Vec<wire::RecallState>) {
        self.handshake_with(
            1,
            feature::PARK | feature::POPULATE_DIR | feature::WRITE_CLOSED | feature::NS_HINT,
        )
        .await
    }

    /// [`Self::handshake`] offering `proto_minor` and `features` (M5: a
    /// v0.2 FSAL offers `CHUNK_MAP`).
    pub async fn handshake_with(
        &mut self,
        proto_minor: u16,
        features: u32,
    ) -> (wire::HelloAck, Vec<wire::RecallState>) {
        self.send(&Message::Hello(Hello {
            proto_major: 0,
            proto_minor,
            features,
            pid: 4242,
            instance: 99,
            ganesha_version: b"V15.6".to_vec(),
            fsal_build: b"test".to_vec(),
        }))
        .await;
        let Message::HelloAck(ack) = self.recv().await.msg else {
            panic!("expected HELLO_ACK")
        };
        assert_eq!(self.recv().await.msg, Message::ResyncBegin);
        let mut states = Vec::new();
        loop {
            match self.recv().await.msg {
                Message::RecallState(s) => states.push(s),
                Message::ResyncEnd => break,
                other => panic!("unexpected {other:?} during resync"),
            }
        }
        (ack, states)
    }
}
