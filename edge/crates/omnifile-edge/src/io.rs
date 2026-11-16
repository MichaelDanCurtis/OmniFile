//! Adapters from the daemon's ports to the Edge-I/O crates:
//! [`omnifile_control::ControlClient`] and [`omnifile_blob::BlobClient`].
//!
//! Policy kept here, not in the clients: uploads never set an access tier
//! (a blob with an explicit tier never enters Smart). S3 is read only: by
//! default the edge signs the service's `s3_roles_anywhere` pointer itself
//! (SigV4, Roles Anywhere credentials from the local helper, M5); a
//! presigned URL is the operator's fallback.
//!
//! Uploads go through the durable [`UploadOutbox`] (`[service] outbox_dir`,
//! default `/var/lib/omnifile/outbox`): one record per `op_id`, so a restart
//! resumes an upload where it stopped and a replay never makes a second
//! version.

use std::io;
use std::path::PathBuf;

use omnifile_blob::{
    Addressing, BlobClient, BlobOptions, CommitProps, CredentialSource, PosixMeta, S3Client,
    S3Object, S3Options, UploadRequest,
};
use omnifile_control::outbox::UploadOp;
use omnifile_control::types as ct;
use omnifile_control::{
    ControlClient, ControlConfig, ControlError, NewUpload, UploadOutbox, UploadState,
};
use omnifile_integrity::BlockPlan;

use crate::ports::*;

fn ctl(e: ControlError) -> CtlError {
    CtlError {
        unavailable: e.is_unavailable(),
        code: e.code().map(str::to_owned),
        // Errors may quote a URL: never pass a SAS signature on.
        message: omnifile_control::util::redact_sas(&e.to_string()),
    }
}

/// The control API through `omnifile-control`, with the uploads outbox.
pub struct ServiceControl {
    pub client: ControlClient,
    outbox: tokio::sync::Mutex<UploadOutbox>,
    sink: BlobClient,
}

impl ServiceControl {
    /// From the `[service]` table: `identity_dir` (default
    /// `/var/lib/omnifile/identity`), `outbox_dir` (default
    /// `/var/lib/omnifile/outbox`) and an optional `api_url`.
    pub fn from_config(service: &toml::Table) -> Result<ServiceControl, String> {
        let s = |k: &str, d: &str| {
            service
                .get(k)
                .and_then(|v| v.as_str())
                .unwrap_or(d)
                .to_owned()
        };
        let dir = s("identity_dir", "/var/lib/omnifile/identity");
        let cfg = ControlConfig {
            api_url_override: service
                .get("api_url")
                .and_then(|v| v.as_str())
                .map(str::to_owned),
            ..ControlConfig::default()
        };
        let client = ControlClient::open(PathBuf::from(dir), cfg).map_err(|e| e.to_string())?;
        let outbox = UploadOutbox::open(s("outbox_dir", "/var/lib/omnifile/outbox"))
            .map_err(|e| format!("outbox: {e}"))?;
        let sink = BlobClient::new(blob_options()).map_err(|e| e.to_string())?;
        Ok(ServiceControl {
            client,
            outbox: tokio::sync::Mutex::new(outbox),
            sink,
        })
    }

    pub fn edge_id(&self) -> u64 {
        self.client.edge_id()
    }
}

fn outbox_upload(op: &UploadOp) -> OutboxUpload {
    let u = &op.upload;
    OutboxUpload {
        op_id: u.op_id.clone(),
        file_id: match &u.file {
            ct::FileRef::Id { file_id } => *file_id,
            _ => 0,
        },
        clone: u.source.clone(),
        size: u.size,
        mtime_ns: u.mtime_ns,
        blake3_hex: u.blake3.clone(),
        crc64nvme: u.crc64nvme,
        block_size: u.block_size,
        block_crcs: u.block_crcs.clone(),
        base_version_id: u.base_version_id,
        uid: u.uid,
        gid: u.gid,
        mode: u.mode,
    }
}

/// The terminal outcome of an op, or `None` while it is still in flight.
fn outcome(state: &UploadState) -> Option<OutboxOutcome> {
    Some(match state {
        UploadState::Committed { version_id, .. } | UploadState::Deduped { version_id } => {
            OutboxOutcome::Committed {
                version_id: *version_id,
            }
        }
        UploadState::Conflict { code, detail } => OutboxOutcome::Conflict {
            detail: detail.clone().unwrap_or_else(|| code.clone()),
        },
        UploadState::Failed { detail } => OutboxOutcome::Failed {
            detail: detail.clone(),
        },
        _ => return None,
    })
}

fn sas(c: &ct::Credential) -> CtlResult<String> {
    if c.is_unsupported() {
        tracing::error!("credential has an unknown mode: unsupported backend, upgrade the edge");
        return Err(CtlError::rejected(
            "unsupported_backend",
            omnifile_control::outbox::UNSUPPORTED_CREDENTIAL,
        ));
    }
    c.sas_url().map(str::to_owned).ok_or_else(|| {
        CtlError::rejected(
            "unsupported_credential",
            "only Azure SAS credentials are read in M1",
        )
    })
}

/// A credential this edge can read with: an Azure SAS, the S3 pointer it
/// signs itself (as a pointer string, `omnifile_blob::s3::POINTER_PREFIX`),
/// or an S3 presigned URL.
pub fn read_url_of(c: &ct::Credential) -> Option<ReadUrl> {
    if let ct::Credential::S3RolesAnywhere {
        bucket,
        key,
        version_id,
        etag,
        crc64nvme,
        region,
        endpoint,
        addressing,
        tls_max_version,
        requester_pays,
        private_network,
    } = c
    {
        let o = S3Object {
            bucket: bucket.clone(),
            key: key.clone(),
            version_id: version_id.clone().filter(|v| !v.is_empty()),
            etag: etag.clone().filter(|e| !e.trim().is_empty()),
            region: region.clone(),
            endpoint: endpoint.clone(),
            // An addressing this edge doesn't know is not guessed at.
            addressing: Addressing::parse(addressing)?,
            tls12_only: tls_max_version.as_deref() == Some("1.2"),
            requester_pays: *requester_pays,
            private_network: *private_network,
        };
        return Some(ReadUrl {
            url: o.to_pointer(),
            source: SourceKind::S3Signed,
            crc64nvme: *crc64nvme,
        });
    }
    if let ct::Credential::S3PresignedUrl {
        url,
        crc64nvme,
        if_match: Some(etag),
        ..
    } = c
    {
        // The URL's signature covers If-Match: carry it with the URL.
        return Some(ReadUrl {
            url: omnifile_blob::PresignedRead {
                url: url.clone(),
                if_match: Some(etag.clone()),
            }
            .encode(),
            source: SourceKind::S3Presigned,
            crc64nvme: *crc64nvme,
        });
    }
    c.read_url().map(|(url, kind)| ReadUrl {
        url: url.to_owned(),
        source: match kind {
            ct::ReadKind::AzureSas => SourceKind::AzureSas,
            ct::ReadKind::S3Presigned => SourceKind::S3Presigned,
        },
        crc64nvme: c.crc64nvme(),
    })
}

fn read(c: &ct::Credential) -> CtlResult<ReadUrl> {
    if c.is_unsupported() {
        tracing::error!("credential has an unknown mode: unsupported backend, upgrade the edge");
        return Err(CtlError::rejected(
            "unsupported_backend",
            omnifile_control::outbox::UNSUPPORTED_CREDENTIAL,
        ));
    }
    read_url_of(c).ok_or_else(|| {
        CtlError::rejected(
            "unsupported_credential",
            "this edge reads Azure SAS, S3 pointers it signs, or S3 presigned URLs",
        )
    })
}

fn word<T: serde::Serialize>(v: &T) -> String {
    serde_json::to_value(v)
        .ok()
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_default()
}

fn export_of(e: ct::SnapshotExport) -> SnapshotExport {
    SnapshotExport {
        export_id: e.export_id,
        state: e.state,
        as_of: e.as_of,
        parts: e
            .parts
            .into_iter()
            .map(|p| ExportPart {
                url: p.url,
                rows: p.rows,
                bytes: p.bytes,
                sha256: p.sha256,
            })
            .collect(),
    }
}

impl ControlPlane for ServiceControl {
    async fn feed(&self, after: &str, limit: u32, wait_s: u32) -> CtlResult<FeedPage> {
        let p = self.client.feed(after, limit, wait_s).await.map_err(ctl)?;
        Ok(FeedPage {
            events: p.events,
            next: p.next,
            reset_required: p.reset_required,
        })
    }

    async fn feed_ack(&self, cursor: &str) -> CtlResult<()> {
        self.client.feed_ack(cursor).await.map_err(ctl)
    }

    async fn asof(
        &self,
        dir_id: u64,
        t: &str,
        after: Option<&str>,
        limit: u32,
    ) -> CtlResult<AsofPage> {
        let p = self
            .client
            .asof(dir_id, t, after, limit)
            .await
            .map_err(ctl)?;
        Ok(AsofPage {
            entries: p.entries,
            more: p.more,
        })
    }

    async fn snapshot(
        &self,
        dataset_id: u64,
        after_file_id: Option<u64>,
        limit: u32,
    ) -> CtlResult<SnapshotPage> {
        let p = self
            .client
            .snapshot(dataset_id, after_file_id, limit)
            .await
            .map_err(ctl)?;
        Ok(SnapshotPage {
            as_of: p.as_of,
            dataset: p.dataset,
            shares: p.shares,
            acls: p.acls,
            files: p.files,
            more: p.more,
        })
    }

    async fn snapshot_export_start(
        &self,
        dataset_id: u64,
        nonce: &str,
    ) -> CtlResult<SnapshotExport> {
        let e = self
            .client
            .snapshot_export_start(dataset_id, nonce)
            .await
            .map_err(ctl)?;
        Ok(export_of(e))
    }

    async fn digest(&self, dataset_id: u64) -> CtlResult<Digest> {
        let d = self.client.digest(dataset_id, None).await.map_err(ctl)?;
        let digest = u64::from_str_radix(&d.digest, 16).map_err(|e| CtlError {
            unavailable: false,
            code: None,
            message: format!("digest {:?}: {e}", d.digest),
        })?;
        Ok(Digest {
            as_of: d.as_of,
            live_rows: d.live_rows,
            digest,
        })
    }

    async fn audit(&self, records: &[serde_json::Value]) -> CtlResult<()> {
        self.client.audit(records).await.map_err(ctl)
    }

    async fn heartbeat(&self, h: &Health) -> CtlResult<HeartbeatReply> {
        let req = omnifile_control::types::HeartbeatRequest {
            version: h.version.clone(),
            kernel: h.kernel.clone(),
            guard_state: h
                .guard_state
                .as_deref()
                .and_then(|s| serde_json::from_value(serde_json::Value::String(s.to_owned())).ok()),
            landing_used_bytes: None,
            landing_capacity_bytes: None,
            cache_used_bytes: None,
            outbox_depth: Some(h.outbox_depth),
            feed_cursor: h.feed_cursor.clone(),
            fills_running: Some(h.fills_running),
            recalls_waiting: Some(h.recalls_waiting),
            data_free_bytes: h.data_free_bytes,
            data_capacity_bytes: h.data_capacity_bytes,
            fsal_connections: Some(h.fsal_connections),
            recovery_used: h.recovery_used,
            quiet_default_s: h.quiet_default_s,
            quiet_worm_default_s: h.quiet_worm_default_s,
            quiet_stale_s: h.quiet_stale_s,
            recall_superseded: h.recall_superseded,
        };
        let r = self.client.heartbeat(&req).await.map_err(ctl)?;
        Ok(HeartbeatReply {
            min_edge_version: r.min_edge_version,
            feed_lag_events: r.feed_lag_events,
            certificate_renew_after: r.certificate_renew_after,
        })
    }

    async fn renew_if_due(&self, renew_after: Option<&str>) -> CtlResult<bool> {
        self.client.renew_if_due(renew_after).await.map_err(ctl)
    }

    async fn renew_roles_anywhere_if_due(&self, dir: &std::path::Path) -> CtlResult<bool> {
        let files = omnifile_control::RolesAnywhereFiles::new(dir);
        self.client
            .renew_roles_anywhere_if_due(&files, false)
            .await
            .map_err(ctl)
    }

    async fn snapshot_export(&self, export_id: u64) -> CtlResult<SnapshotExport> {
        let e = self.client.snapshot_export(export_id).await.map_err(ctl)?;
        Ok(export_of(e))
    }

    async fn create(&self, op: &CreateOp) -> CtlResult<Created> {
        let mut c = ct::ChangeOp::new(op.op_id.clone(), ct::ChangeKind::Create);
        c.parent = Some(ct::FileRef::Id {
            file_id: op.parent_file_id,
        });
        c.file = Some(ct::FileRef::Local {
            local: ct::LocalRef {
                fsid: op.fsid.to_string(),
                ino: op.ino.to_string(),
                ino_gen: op.ino_gen.to_string(),
            },
        });
        c.name = Some(op.name.clone());
        c.kind = Some(match op.kind.as_str() {
            "dir" => ct::NodeKind::Dir,
            "symlink" => ct::NodeKind::Symlink,
            _ => ct::NodeKind::File,
        });
        c.attrs = Some(ct::Attrs {
            size: Some(op.size),
            mtime_ns: Some(op.mtime_ns),
            uid: Some(op.uid),
            gid: Some(op.gid),
            mode: Some(op.mode & 0o7777),
            ..ct::Attrs::default()
        });
        c.actor = op.actor.clone();
        // The batch's Idempotency-Key (16-128 characters) derives from the
        // op_id, so a resend after a crash replays rather than re-applies.
        let key = omnifile_control::util::derived_idempotency_key("changes", &op.op_id);
        let results = self.client.changes(&[c], &key).await.map_err(ctl)?;
        let r = results
            .into_iter()
            .next()
            .ok_or_else(|| CtlError::rejected("empty", "no change result"))?;
        match (r.status, r.file_id) {
            (ct::ChangeStatus::Applied | ct::ChangeStatus::Duplicate, Some(file_id)) => {
                Ok(Created {
                    file_id,
                    entity_version: r.entity_version.unwrap_or(0),
                })
            }
            _ => Err(CtlError::rejected(
                r.code.as_deref().unwrap_or("rejected"),
                r.detail.unwrap_or_else(|| format!("{:?}", r.status)),
            )),
        }
    }

    async fn ns_changes(&self, ops: &[NsChange]) -> CtlResult<Vec<NsResult>> {
        let id = |file_id: u64| ct::FileRef::Id { file_id };
        let batch: Vec<ct::ChangeOp> = ops
            .iter()
            .map(|op| match op {
                NsChange::Rename {
                    op_id,
                    file_id,
                    new_parent_file_id,
                    new_name,
                    ..
                } => {
                    let mut c = ct::ChangeOp::new(op_id.clone(), ct::ChangeKind::Rename);
                    c.file = Some(id(*file_id));
                    c.new_parent = Some(id(*new_parent_file_id));
                    c.new_name = Some(new_name.clone());
                    c.replace = Some(false);
                    c
                }
                NsChange::Unlink { op_id, file_id, .. } => {
                    let mut c = ct::ChangeOp::new(op_id.clone(), ct::ChangeKind::Unlink);
                    c.file = Some(id(*file_id));
                    c
                }
                NsChange::Owner {
                    op_id,
                    file_id,
                    uid,
                    gid,
                    ..
                } => {
                    let mut c = ct::ChangeOp::new(op_id.clone(), ct::ChangeKind::Setattr);
                    c.file = Some(id(*file_id));
                    c.attrs = Some(ct::Attrs {
                        uid: Some(*uid),
                        gid: Some(*gid),
                        ..ct::Attrs::default()
                    });
                    c
                }
            })
            .zip(ops)
            .map(|(mut c, op)| {
                c.actor = op.actor().cloned();
                c
            })
            .collect();
        let (Some(first), Some(last)) = (ops.first(), ops.last()) else {
            return Ok(Vec::new());
        };
        // The same ops resent after a crash form the same batch (they are
        // sent oldest first), so they replay under the same key.
        let key = omnifile_control::util::derived_idempotency_key(
            "ns-changes",
            &format!("{}..{}", first.op_id(), last.op_id()),
        );
        let results = self.client.changes(&batch, &key).await.map_err(ctl)?;
        Ok(results
            .into_iter()
            .map(|r| NsResult {
                op_id: r.op_id,
                status: match r.status {
                    ct::ChangeStatus::Applied | ct::ChangeStatus::Duplicate => NsStatus::Applied,
                    ct::ChangeStatus::Conflict => NsStatus::Conflict,
                    _ => NsStatus::Rejected,
                },
                entity_version: r.entity_version,
                code: r.code,
                detail: r.detail,
            })
            .collect())
    }

    async fn upload_start(&self, i: &UploadIntent) -> CtlResult<UploadStart> {
        let intent = ct::UploadIntent {
            op_id: i.op_id.clone(),
            file: ct::FileRef::Id { file_id: i.file_id },
            size: i.size,
            mtime_ns: i.mtime_ns,
            blake3: i.blake3_hex.clone(),
            crc64nvme: i.crc64nvme,
            block_size: i.block_size,
            base_version_id: i.base_version_id,
        };
        match self.client.upload_start(&intent).await.map_err(ctl)? {
            ct::UploadStart::Dedupe { version_id } => Ok(UploadStart::Dedupe { version_id }),
            // `set_tier` is ignored on purpose: the edge never sends a tier.
            ct::UploadStart::Upload {
                upload_id,
                version_id,
                target,
                block_size,
                ..
            } => Ok(UploadStart::Upload {
                upload_id,
                version_id,
                sas_url: sas(&target)?,
                block_size,
            }),
        }
    }

    async fn upload_complete(&self, upload_id: u64, d: &UploadDone) -> CtlResult<u64> {
        let body = ct::UploadComplete {
            crc64nvme: d.crc64nvme,
            blake3: d.blake3_hex.clone(),
            size: d.size,
            etag: Some(d.etag.clone()).filter(|e| !e.is_empty()),
            backend_version_id: d.backend_version_id.clone(),
            // Uploads to the service go through the outbox (`has_outbox`),
            // which declares the committed block list; this direct path is
            // never taken with ServiceControl, and the service would refuse
            // a `complete` without blocks.
            blocks: Vec::new(),
        };
        Ok(self
            .client
            .upload_complete(upload_id, &body)
            .await
            .map_err(ctl)?
            .version_id)
    }

    async fn stub_prepare(&self, file_id: u64, version_id: u64) -> CtlResult<StubPrepare> {
        Ok(
            match self
                .client
                .stub_prepare(file_id, version_id)
                .await
                .map_err(ctl)?
            {
                omnifile_control::StubPrepare::Lease(l) => StubPrepare::Lease {
                    lease_id: l.lease_id,
                    resident_bytes: l.resident_bytes,
                },
                omnifile_control::StubPrepare::Refused { code, .. } => {
                    StubPrepare::Refused { code }
                }
            },
        )
    }

    async fn stub_commit(
        &self,
        file_id: u64,
        lease_id: &str,
        version_id: u64,
        resident_bytes: u64,
    ) -> CtlResult<()> {
        // Opaque (`<expiry>.<MAC hex>`): sent back unchanged.
        self.client
            .stub_commit(file_id, lease_id, version_id, resident_bytes)
            .await
            .map_err(ctl)
    }

    async fn stub_skip(
        &self,
        file_id: u64,
        version_id: u64,
        reason: &str,
        detail: Option<&str>,
    ) -> CtlResult<()> {
        let r = match reason {
            "version_mismatch" | "inode_changed" | "not_managed" => {
                ct::StubSkipReason::VersionMismatch
            }
            "open_for_write" => ct::StubSkipReason::OpenForWrite,
            "stub_not_allowed" => ct::StubSkipReason::LeaseRefused,
            _ => ct::StubSkipReason::LocalError,
        };
        let detail = match detail {
            Some(d) => format!("{reason}: {d}"),
            None => reason.to_string(),
        };
        self.client
            .stub_skip(file_id, version_id, r, Some(&detail))
            .await
            .map_err(ctl)
    }

    async fn recall_start(&self, req: &RecallRequest) -> CtlResult<RecallResponse> {
        let sec = req.sec.as_deref().and_then(|s| match s {
            "sys" => Some(ct::Sec::Sys),
            "krb5" => Some(ct::Sec::Krb5),
            "krb5i" => Some(ct::Sec::Krb5i),
            "krb5p" => Some(ct::Sec::Krb5p),
            _ => None,
        });
        let r = ct::RecallRequest {
            file_id: req.file_id,
            version_id: req.version_id,
            offset: Some(req.offset),
            length: Some(req.length),
            requester: ct::Requester {
                uid: req.uid,
                sid: req.sid.clone(),
                via: match req.via.as_str() {
                    "smb" => ct::Via::Smb,
                    "local" => ct::Via::Local,
                    _ => ct::Via::Nfs,
                },
                sec,
                proof: if req.proof == "proven" {
                    ct::Proof::Proven
                } else {
                    ct::Proof::Claimed
                },
                client_addr: req.client_addr.clone(),
            },
            on_open: match req.on_open.as_str() {
                "wait" => ct::OnOpen::Wait,
                "auto" => ct::OnOpen::Auto,
                _ => ct::OnOpen::Fail,
            },
            pinned: req.pinned,
        };
        Ok(match self.client.recall_start(&r).await.map_err(ctl)? {
            ct::RecallResponse::Online { credential } => RecallResponse::Online {
                url: read(&credential)?,
            },
            ct::RecallResponse::Recall {
                recall_id,
                state,
                decision,
                note,
                ..
            } => RecallResponse::Recall {
                recall_id,
                state: word(&state),
                decision: word(&decision),
                // No time is promised the FSAL can't verify; the card shows the ETA.
                eta_s: None,
                note: Some(note).filter(|n| !n.is_empty()),
            },
        })
    }

    async fn recall_filled(
        &self,
        recall_id: u64,
        version_id: u64,
        resident_bytes: u64,
    ) -> CtlResult<()> {
        self.client
            .recall_filled(recall_id, version_id, resident_bytes)
            .await
            .map_err(ctl)
    }

    async fn recall_failed(&self, recall_id: u64, version_id: u64, reason: &str) -> CtlResult<()> {
        self.client
            .recall_failed(recall_id, version_id, reason)
            .await
            .map_err(ctl)
    }

    async fn file_filled(
        &self,
        file_id: u64,
        version_id: u64,
        resident_bytes: u64,
    ) -> CtlResult<()> {
        self.client
            .file_filled(file_id, version_id, resident_bytes)
            .await
            .map_err(ctl)
    }

    async fn acl_put(&self, dataset_id: u64, acls: Vec<serde_json::Value>) -> CtlResult<()> {
        for chunk in acls.chunks(1000) {
            self.client.put_acls(dataset_id, chunk).await.map_err(ctl)?;
        }
        Ok(())
    }

    async fn acl_scan_start(&self, dataset_id: u64, kind: &str, nonce: &str) -> CtlResult<u64> {
        self.client
            .acl_scan_start(dataset_id, kind, nonce)
            .await
            .map_err(ctl)
    }

    async fn acl_scan_complete(&self, scan_id: u64, seen: u64, changed: u64) -> CtlResult<()> {
        self.client
            .acl_scan_complete(scan_id, seen, changed)
            .await
            .map_err(ctl)
    }

    async fn acl_set(&self, batch_id: &str, ops: Vec<(u64, String, u32)>) -> CtlResult<usize> {
        let mut applied = 0;
        for (n, chunk) in ops.chunks(500).enumerate() {
            let changes: Vec<ct::ChangeOp> = chunk
                .iter()
                .enumerate()
                .map(|(i, (file_id, sha, mode))| {
                    let mut c =
                        ct::ChangeOp::new(format!("{batch_id}-{n}-{i}"), ct::ChangeKind::Setattr);
                    c.file = Some(ct::FileRef::Id { file_id: *file_id });
                    c.attrs = Some(ct::Attrs {
                        mode: Some(*mode & 0o7777),
                        acl_sha256: Some(sha.clone()),
                        ..ct::Attrs::default()
                    });
                    c
                })
                .collect();
            let key = omnifile_control::util::derived_idempotency_key(
                "changes",
                &format!("{batch_id}-{n}"),
            );
            let results = self.client.changes(&changes, &key).await.map_err(ctl)?;
            for r in results {
                match r.status {
                    ct::ChangeStatus::Applied | ct::ChangeStatus::Duplicate => applied += 1,
                    other => tracing::warn!(
                        status = ?other,
                        code = ?r.code,
                        detail = ?r.detail,
                        "acl change op not applied"
                    ),
                }
            }
        }
        Ok(applied)
    }

    async fn read_url(&self, file_id: u64, version_id: u64) -> CtlResult<ReadUrl> {
        read(
            &self
                .client
                .read_credential(file_id, version_id)
                .await
                .map_err(ctl)?,
        )
    }

    fn has_outbox(&self) -> bool {
        true
    }

    async fn outbox_commit(&self, up: &OutboxUpload) -> CtlResult<OutboxOutcome> {
        let mut ob = self.outbox.lock().await;
        ob.enqueue(NewUpload {
            op_id: up.op_id.clone(),
            file: ct::FileRef::Id {
                file_id: up.file_id,
            },
            source: up.clone.clone(),
            size: up.size,
            mtime_ns: up.mtime_ns,
            blake3: up.blake3_hex.clone(),
            crc64nvme: up.crc64nvme,
            block_size: up.block_size,
            block_crcs: up.block_crcs.clone(),
            base_version_id: up.base_version_id,
            uid: up.uid,
            gid: up.gid,
            mode: up.mode,
        })
        .map_err(ctl)?;
        ob.drive(&up.op_id, &self.client, &self.sink)
            .await
            .map_err(ctl)?;
        ob.get(&up.op_id)
            .and_then(|op| outcome(&op.state))
            .ok_or_else(|| CtlError::unavailable("upload still in flight"))
    }

    async fn outbox_resume(&self) -> CtlResult<Vec<(OutboxUpload, OutboxOutcome)>> {
        let mut ob = self.outbox.lock().await;
        if !ob.pending().is_empty()
            && let Err(e) = ob.drain(&self.client, &self.sink).await
        {
            tracing::info!(err = %omnifile_control::util::redact_sas(&e.to_string()), waiting = ob.pending().len(), "upload outbox: still waiting");
        }
        Ok(ob
            .finished()
            .into_iter()
            .filter_map(|op| outcome(&op.state).map(|o| (outbox_upload(op), o)))
            .collect())
    }

    async fn outbox_holds(&self, file_id: u64) -> bool {
        let ob = self.outbox.lock().await;
        ob.pending()
            .into_iter()
            .chain(ob.finished())
            .any(|op| matches!(op.upload.file, ct::FileRef::Id { file_id: f } if f == file_id))
    }

    async fn outbox_clones(&self) -> Vec<std::path::PathBuf> {
        let ob = self.outbox.lock().await;
        ob.pending()
            .into_iter()
            .chain(ob.finished())
            .map(|op| op.upload.source.clone())
            .collect()
    }

    async fn outbox_forget(&self, op_id: &str) -> CtlResult<()> {
        self.outbox.lock().await.remove(op_id).map_err(ctl)
    }
}

/// Blob client options: https only, unless `OMNIFILE_BLOB_ALLOW_HTTP=1`
/// (the local sim and the e2e runner, never an appliance).
pub fn blob_options() -> BlobOptions {
    let allow_http = std::env::var("OMNIFILE_BLOB_ALLOW_HTTP").is_ok_and(|v| v == "1");
    if allow_http {
        tracing::warn!("OMNIFILE_BLOB_ALLOW_HTTP=1: plain-http blob URLs accepted (test/sim only)");
    }
    BlobOptions {
        allow_http,
        ..BlobOptions::default()
    }
}

/// The S3 reader's options from `[s3]`: credentials from the Roles
/// Anywhere helper on loopback; the sim's plain-http hosts only with
/// `OMNIFILE_BLOB_ALLOW_HTTP=1`.
pub fn s3_options(cfg: &crate::config::S3Config) -> S3Options {
    let allow_http = std::env::var("OMNIFILE_BLOB_ALLOW_HTTP").is_ok_and(|v| v == "1");
    S3Options {
        credentials: CredentialSource::Imds {
            url: cfg.imds_url.clone(),
        },
        allow_http,
        test_endpoints: if allow_http {
            cfg.test_endpoints.clone()
        } else {
            Vec::new()
        },
        ..S3Options::default()
    }
}

/// Blob data plane through `omnifile-blob`: Azure, and S3 reads.
pub struct ServiceBlob {
    pub client: BlobClient,
    pub s3: S3Client,
}

impl ServiceBlob {
    pub fn new(s3: &crate::config::S3Config) -> Result<ServiceBlob, String> {
        Ok(ServiceBlob {
            client: BlobClient::new(blob_options()).map_err(|e| e.to_string())?,
            s3: S3Client::new(s3_options(s3)).map_err(|e| format!("S3 reader: {e}"))?,
        })
    }
}

impl BlobIo for ServiceBlob {
    async fn get_range(
        &self,
        url: &str,
        source: SourceKind,
        offset: u64,
        len: u64,
    ) -> io::Result<Vec<u8>> {
        let b = match source {
            SourceKind::AzureSas => self.client.get_range(url, offset, len).await,
            SourceKind::S3Presigned => self.client.get_s3_range(url, offset, len).await,
            SourceKind::S3Signed => match S3Object::from_pointer(url) {
                Ok(o) => self.s3.get_range(&o, offset, len).await,
                Err(e) => Err(e),
            },
        }
        .map_err(|e| {
            // A refused credential (an expired URL) is told apart: the fill
            // asks for a new one and resumes. The `BlobError` stays inside,
            // so the cache tells archived, changed (412) and unsupported
            // sources apart.
            let kind = if e.is_auth() {
                io::ErrorKind::PermissionDenied
            } else {
                io::ErrorKind::Other
            };
            io::Error::new(kind, e)
        })?;
        Ok(b.to_vec())
    }

    async fn upload(&self, req: &BlobUpload) -> io::Result<BlobUploaded> {
        let plan = BlockPlan::with_block_size(req.size, req.block_size)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, format!("{e:?}")))?;
        let mut journal = req.path.clone().into_os_string();
        journal.push(".journal");
        let journal = PathBuf::from(journal);
        let r = UploadRequest {
            sas_url: req.sas_url.clone(),
            source: req.path.clone(),
            plan,
            expected_block_crcs: Some(req.block_crcs.clone()),
            journal: journal.clone(),
            fingerprint: req
                .path
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default(),
            props: CommitProps {
                metadata: PosixMeta {
                    uid: req.uid,
                    gid: req.gid,
                    mode: req.mode,
                }
                .to_metadata(),
                access_tier: None,
                content_type: None,
            },
        };
        let out = self
            .client
            .upload_file(&r)
            .await
            .map_err(|e| io::Error::other(e.to_string()))?;
        // The journal stays until `uploads/complete` succeeded: the commit
        // pipeline removes it with the clone (N5).
        Ok(BlobUploaded {
            etag: out.etag.unwrap_or_default(),
            version_id: out.version_id,
            crc64nvme: out.crc64nvme,
            size: out.size,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_credential_mode_is_a_clean_unsupported_backend_error() {
        let cred: ct::Credential =
            serde_json::from_str(r#"{"mode":"nfs_nas_mount","host":"nas1"}"#).unwrap();
        let e = sas(&cred).unwrap_err();
        assert_eq!(e.code.as_deref(), Some("unsupported_backend"));
        assert!(!e.unavailable, "not retryable: only an upgrade fixes it");
        assert!(e.message.contains("upgrade the edge"), "{}", e.message);
    }
}
