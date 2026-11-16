//! Round trips against the sim Azure Blob facade (`sim_http`).

mod support;

use std::path::Path;
use std::time::{Duration, Instant};

use bytes::Bytes;
use omnifile_blob::{
    BlobClient, BlobError, BlobOptions, CommitProps, PosixMeta, UploadJournal, UploadRequest,
};
use omnifile_integrity::{BlockPlan, block_id, crc64_nvme};
use support::sim::Sim;

const KIB: u64 = 1024;
const MIB: u64 = 1024 * KIB;

fn data(len: usize, seed: u64) -> Vec<u8> {
    let mut rng = fastrand::Rng::with_seed(seed);
    (0..len).map(|_| rng.u8(..)).collect()
}

fn client(concurrency: usize) -> BlobClient {
    BlobClient::new(BlobOptions {
        concurrency,
        // The sim speaks plain http.
        allow_http: true,
        ..BlobOptions::default()
    })
    .unwrap()
}

fn request(sas: &str, src: &Path, plan: BlockPlan, journal: &Path) -> UploadRequest {
    UploadRequest {
        sas_url: sas.to_owned(),
        source: src.to_owned(),
        plan,
        expected_block_crcs: None,
        journal: journal.to_owned(),
        fingerprint: "fp-test".into(),
        props: CommitProps {
            metadata: PosixMeta {
                uid: 41023,
                gid: 513,
                mode: 0o100640,
            }
            .to_metadata(),
            ..Default::default()
        },
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn upload_get_range_and_properties() {
    let Some(sim) = Sim::start() else { return };
    let dir = tempfile::tempdir().unwrap();
    let bytes = data((5 * MIB + 12_345) as usize, 1);
    let src = dir.path().join("line_1042.segy");
    std::fs::write(&src, &bytes).unwrap();
    let plan = BlockPlan::with_block_size(bytes.len() as u64, MIB).unwrap();
    let expected: Vec<u64> = plan
        .blocks()
        .map(|b| crc64_nvme(&bytes[b.offset as usize..(b.offset + b.len) as usize]))
        .collect();
    let sas = sim.sas("data", "obj/0001", "rcw", 600).await;
    let mut req = request(&sas, &src, plan, &dir.path().join("u.journal"));
    req.expected_block_crcs = Some(expected);

    let out = client(3).upload_file(&req).await.unwrap();
    assert_eq!(out.size, bytes.len() as u64);
    assert_eq!(
        out.crc64nvme,
        crc64_nvme(&bytes),
        "combined CRC = whole-file CRC"
    );
    assert_eq!(out.blocks_uploaded, plan.count());
    assert!(out.etag.is_some());

    let c = client(1);
    let props = c.get_properties(&sas).await.unwrap();
    assert_eq!(props.size, bytes.len() as u64);
    // No explicit tier was sent, so the live account's default applies: Smart.
    assert_eq!(props.access_tier.as_deref(), Some("Smart"));
    assert_eq!(
        props.metadata.get("file_owner").map(String::as_str),
        Some("41023")
    );
    assert_eq!(
        props.metadata.get("file_group").map(String::as_str),
        Some("513")
    );
    assert_eq!(
        props.metadata.get("file_permissions").map(String::as_str),
        Some("0640")
    );

    for (off, len) in [
        (0u64, 4096u64),
        (MIB - 10, 20),
        (3 * MIB, 4 * MIB), // range CRC checked (<= 4 MiB)
        (0, 5 * MIB),       // too big for a range CRC
        (5 * MIB, 12_345),  // the short last block
        (bytes.len() as u64 - 1, 1),
    ] {
        let got = c
            .get_range(&sas, off, len.min(bytes.len() as u64 - off))
            .await
            .unwrap();
        assert_eq!(
            &got[..],
            &bytes[off as usize..(off + got.len() as u64) as usize]
        );
    }

    // A second call with the committed journal sends nothing and returns the same result.
    let again = client(3).upload_file(&req).await.unwrap();
    assert_eq!(again.blocks_uploaded, 0);
    assert_eq!(again.etag, out.etag);
    assert_eq!(again.crc64nvme, out.crc64nvme);
}

#[tokio::test(flavor = "multi_thread")]
async fn bad_crc_is_rejected_by_the_service() {
    let Some(sim) = Sim::start() else { return };
    let sas = sim.sas("data", "obj/badcrc", "rcw", 600).await;
    let body = Bytes::from(data(64 * KIB as usize, 2));
    let good = crc64_nvme(&body);
    let err = client(1)
        .put_block(&sas, &block_id(7, 0), body.clone(), good ^ 1)
        .await
        .unwrap_err();
    assert_eq!(err.status(), Some(400), "{err}");
    assert_eq!(err.code(), Some("Crc64Mismatch"), "{err}");
    assert!(!err.is_retryable());
    // The right CRC is accepted.
    client(1)
        .put_block(&sas, &block_id(7, 0), body, good)
        .await
        .unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn changed_source_is_caught_before_upload() {
    let Some(sim) = Sim::start() else { return };
    let dir = tempfile::tempdir().unwrap();
    let bytes = data(3 * MIB as usize, 3);
    let src = dir.path().join("f");
    std::fs::write(&src, &bytes).unwrap();
    let plan = BlockPlan::with_block_size(bytes.len() as u64, MIB).unwrap();
    let sas = sim.sas("data", "obj/changed", "rcw", 600).await;
    let mut req = request(&sas, &src, plan, &dir.path().join("j"));
    req.expected_block_crcs = Some(vec![1, 2, 3]);
    let err = client(2).upload_file(&req).await.unwrap_err();
    assert!(matches!(err, BlobError::Integrity(_)), "{err}");
    let err = client(1).get_properties(&sas).await.unwrap_err();
    assert_eq!(err.status(), Some(404), "nothing was committed: {err}");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_closed_gate_commits_nothing() {
    let Some(sim) = Sim::start() else { return };
    let dir = tempfile::tempdir().unwrap();
    let bytes = data(2 * MIB as usize, 4);
    let src = dir.path().join("f");
    std::fs::write(&src, &bytes).unwrap();
    let plan = BlockPlan::with_block_size(bytes.len() as u64, MIB).unwrap();
    let sas = sim.sas("data", "obj/gated", "rcw", 600).await;
    let req = request(&sas, &src, plan, &dir.path().join("j"));
    let asked = std::sync::atomic::AtomicU32::new(0);
    type Fut<'a> =
        std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), String>> + Send + 'a>>;
    let gate = || -> Fut<'_> {
        asked.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Box::pin(async { Err("the source changed at the last moment".to_owned()) })
    };
    let err = client(2).upload_file_gated(&req, &gate).await.unwrap_err();
    assert!(matches!(err, BlobError::Integrity(_)), "{err}");
    assert_eq!(asked.load(std::sync::atomic::Ordering::SeqCst), 1);
    // Blocks were staged, but no block list: no blob exists.
    let err = client(1).get_properties(&sas).await.unwrap_err();
    assert_eq!(err.status(), Some(404), "nothing was committed: {err}");
    // An open gate commits as usual.
    let ok = || -> Fut<'_> { Box::pin(async { Ok(()) }) };
    let out = client(2).upload_file_gated(&req, &ok).await.unwrap();
    assert_eq!(out.size, bytes.len() as u64);
}

#[tokio::test(flavor = "multi_thread")]
async fn empty_file_commits_an_empty_block_list() {
    let Some(sim) = Sim::start() else { return };
    let dir = tempfile::tempdir().unwrap();
    let src = dir.path().join("empty");
    std::fs::write(&src, b"").unwrap();
    let sas = sim.sas("data", "obj/empty", "rcw", 600).await;
    let req = request(
        &sas,
        &src,
        BlockPlan::new(0).unwrap(),
        &dir.path().join("j"),
    );
    let out = client(1).upload_file(&req).await.unwrap();
    assert_eq!(out.size, 0);
    assert_eq!(out.crc64nvme, 0);
    assert_eq!(client(1).get_properties(&sas).await.unwrap().size, 0);
}

#[tokio::test(flavor = "multi_thread")]
async fn resume_skips_blocks_in_the_journal() {
    let Some(sim) = Sim::start() else { return };
    let dir = tempfile::tempdir().unwrap();
    let bytes = data((4 * MIB + 7) as usize, 4);
    let src = dir.path().join("f");
    std::fs::write(&src, &bytes).unwrap();
    let plan = BlockPlan::with_block_size(bytes.len() as u64, MIB).unwrap();
    let sas = sim.sas("data", "obj/resume", "rcw", 600).await;
    let jpath = dir.path().join("j");
    let c = client(2);
    {
        // A previous run staged blocks 0 and 2, then died.
        let target = omnifile_blob::strip_query(&sas).unwrap();
        let mut j = UploadJournal::open(&jpath, &target, plan.size(), plan.block_size(), "fp-test")
            .unwrap();
        for i in [0u32, 2] {
            let b = plan.block(i).unwrap();
            let body =
                Bytes::copy_from_slice(&bytes[b.offset as usize..(b.offset + b.len) as usize]);
            let crc = crc64_nvme(&body);
            c.put_block(&sas, &block_id(j.tag(), i), body, crc)
                .await
                .unwrap();
            j.mark_block(i, crc).unwrap();
        }
    }
    let out = c
        .upload_file(&request(&sas, &src, plan, &jpath))
        .await
        .unwrap();
    assert_eq!(out.blocks_resumed, 2);
    assert_eq!(out.blocks_uploaded, plan.count() - 2);
    assert_eq!(out.crc64nvme, crc64_nvme(&bytes));
    let got = c.get_range(&sas, 0, bytes.len() as u64).await.unwrap();
    assert_eq!(&got[..], &bytes[..]);
}

#[tokio::test(flavor = "multi_thread")]
async fn lost_staged_blocks_restart_the_upload() {
    let Some(sim) = Sim::start() else { return };
    let dir = tempfile::tempdir().unwrap();
    let bytes = data((2 * MIB) as usize, 5);
    let src = dir.path().join("f");
    std::fs::write(&src, &bytes).unwrap();
    let plan = BlockPlan::with_block_size(bytes.len() as u64, MIB).unwrap();
    let sas = sim.sas("data", "obj/lost", "rcw", 600).await;
    let jpath = dir.path().join("j");
    {
        // The journal claims block 0 is staged, but Azure never got it.
        let target = omnifile_blob::strip_query(&sas).unwrap();
        let mut j = UploadJournal::open(&jpath, &target, plan.size(), plan.block_size(), "fp-test")
            .unwrap();
        j.mark_block(0, crc64_nvme(&bytes[..MIB as usize])).unwrap();
    }
    let out = client(2)
        .upload_file(&request(&sas, &src, plan, &jpath))
        .await
        .unwrap();
    assert_eq!(
        out.blocks_uploaded,
        plan.count(),
        "everything re-sent after InvalidBlockList"
    );
    assert_eq!(out.crc64nvme, crc64_nvme(&bytes));
}

// ---- resume after SIGKILL ------------------------------------------------

const CHILD_ENV: &str = "OMNIFILE_BLOB_KILL_CHILD";

/// Runs only as the child of `resume_after_kill`: a slow upload to be killed.
#[tokio::test(flavor = "multi_thread")]
async fn child_upload_for_kill_test() {
    let Ok(spec) = std::env::var(CHILD_ENV) else {
        return;
    };
    let v: serde_json::Value = serde_json::from_str(&spec).unwrap();
    let src = Path::new(v["src"].as_str().unwrap());
    let size = std::fs::metadata(src).unwrap().len();
    let plan = BlockPlan::with_block_size(size, v["block_size"].as_u64().unwrap()).unwrap();
    let c = BlobClient::new(BlobOptions {
        concurrency: 1,
        max_upload_bytes_per_sec: Some(v["rate"].as_u64().unwrap()),
        allow_http: true,
        ..BlobOptions::default()
    })
    .unwrap();
    let req = request(
        v["sas"].as_str().unwrap(),
        src,
        plan,
        Path::new(v["journal"].as_str().unwrap()),
    );
    c.upload_file(&req).await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn resume_after_kill() {
    if std::env::var(CHILD_ENV).is_ok() {
        return;
    }
    let Some(sim) = Sim::start() else { return };
    let dir = tempfile::tempdir().unwrap();
    let block = 64 * KIB;
    let bytes = data((24 * block + 99) as usize, 6);
    let src = dir.path().join("f");
    std::fs::write(&src, &bytes).unwrap();
    let jpath = dir.path().join("j");
    let sas = sim.sas("data", "obj/killed", "rcw", 600).await;
    let spec = serde_json::json!({
        "src": src, "journal": jpath, "sas": sas, "block_size": block,
        // ~8 blocks per second: the whole upload would take ~3 s.
        "rate": 8 * block,
    });
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "child_upload_for_kill_test",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(CHILD_ENV, spec.to_string())
        .stdout(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        let lines = std::fs::read_to_string(&jpath)
            .map(|s| s.lines().count())
            .unwrap_or(0);
        if lines > 4 {
            break; // header + 4 durable blocks
        }
        assert!(Instant::now() < deadline, "child made no progress");
        assert!(
            child.try_wait().unwrap().is_none(),
            "child exited before it was killed"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
    child.kill().unwrap(); // SIGKILL
    child.wait().unwrap();

    let plan = BlockPlan::with_block_size(bytes.len() as u64, block).unwrap();
    let c = client(4);
    let out = c
        .upload_file(&request(&sas, &src, plan, &jpath))
        .await
        .unwrap();
    assert!(out.blocks_resumed >= 4, "resumed {}", out.blocks_resumed);
    assert!(
        out.blocks_resumed < plan.count(),
        "child was killed before finishing"
    );
    assert_eq!(out.blocks_resumed + out.blocks_uploaded, plan.count());
    assert_eq!(out.crc64nvme, crc64_nvme(&bytes));
    let got = c.get_range(&sas, 0, bytes.len() as u64).await.unwrap();
    assert_eq!(&got[..], &bytes[..]);
}

#[tokio::test(flavor = "multi_thread")]
async fn read_only_sas_cannot_write_and_missing_blob_is_404() {
    let Some(sim) = Sim::start() else { return };
    let sas = sim.sas("data", "obj/ro", "r", 600).await;
    let body = Bytes::from_static(b"hello");
    let err = client(1)
        .put_block(&sas, &block_id(1, 0), body.clone(), crc64_nvme(&body))
        .await
        .unwrap_err();
    assert_eq!(err.status(), Some(403), "{err}");
    assert!(err.is_auth());
    let err = client(1).get_range(&sas, 0, 5).await.unwrap_err();
    assert_eq!(err.status(), Some(404), "{err}");
}
