//! A small CLI over `omnifile-acl` and `omnifile-identity`, used by the
//! permission matrix (`tests/acl/matrix.sh`) on a real edge:
//!
//! ```text
//! omniacl apply-root <dir> <owners_gid> [writers_gid|-] [readers_gid|-]
//! omniacl check-root <dir> <owners_gid> [writers_gid|-] [readers_gid|-]
//! omniacl retarget   <dir> <from_gid> <to_gid|->
//! omniacl normalize  <dir>                  # other::---, mask = group class
//! omniacl scan       <dir> [state.json]   # prints changed files since state.json
//! omniacl render     <name> <path> <export_id> <protocols> <nfs_sec> <clients|-> <owners_sid> <writers_sid|-> <readers_sid|-> <policy> [snapdir]
//!   snapdir: the share's rewind tree (`<data>/.omni/snap/<name>`), shown to SMB and NFS clients
//!   as Previous Versions (`vfs_shadow_copy2`); omitted or `-`: none.
//!   With a snapdir, `vfs_omnifile_prevver` (`samba-vfs/`) is stacked before `shadow_copy2`
//!   when smbd has it: `$OMNIACL_SMB_PREVVER` is `on` or `off`; unset or `auto`, it is used
//!   if `<smbd -b MODULESDIR>/vfs/omnifile_prevver.so` exists.
//! omniacl check-prevver [--build-only]
//!   proves smbd can load vfs_omnifile_prevver: its baked-in VFS interface version and Samba
//!   release against the running smbd, then a real load (a throwaway smbd serves a scratch
//!   share that uses it, `smbclient ls`; needs root). Exit 0 ok, 1 not usable (the message
//!   names the module and both Samba versions). `--build-only` skips the load.
//! omniacl resolve-nfs <uid> <sys|krb5|krb5i|krb5p> [client_addr]
//! omniacl resolve-smb <pid>
//! ```
//!
//! Ids resolve through winbind (`wbinfo`), sessions through `smbstatus`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use omnifile_acl::{
    AclFs, FsalPolicy, Protocol, ShareConfig, ShareGroups, SysFs, apply_root, check_root,
    retarget_group, scan,
};
use omnifile_identity::{
    CachedIdMap, IdentityResolver, NfsRequest, NfsSec, Resolver, ResolverConfig, Sid, SmbRequest,
    TrustLevel, Wbinfo,
};

fn die(msg: &str) -> ! {
    eprintln!("omniacl: {msg}");
    std::process::exit(2)
}

/// Whether a stanza may name vfs_omnifile_prevver. Where it is installed (or
/// forced `on` with an smbd here), it must also be loadable: build info against
/// the running smbd, then a real load in a throwaway smbd. A module smbd would
/// refuse is an error here, not a stanza that stops the share connecting.
fn smb_prevver() -> bool {
    use omnifile_acl::smb_module::{PrevverError, check_prevver};
    let forced = match std::env::var("OMNIACL_SMB_PREVVER").ok().as_deref() {
        Some("on") => true,
        Some("off") => return false,
        None | Some("") | Some("auto") => false,
        Some(v) => die(&format!("OMNIACL_SMB_PREVVER: {v:?} (on, off or auto)")),
    };
    match check_prevver() {
        Ok(ok) => {
            eprintln!("omniacl: {ok}");
            true
        }
        // No smbd here (a staging host): nothing to check against.
        Err(PrevverError::NoSmbd(_)) => forced,
        Err(PrevverError::NotInstalled(_)) if !forced => false,
        Err(e) => die(&e.to_string()),
    }
}

fn opt_u32(s: Option<&String>) -> Option<u32> {
    s.filter(|s| s.as_str() != "-")
        .map(|s| s.parse().unwrap_or_else(|_| die("bad id")))
}

fn groups(a: &[String]) -> ShareGroups {
    ShareGroups {
        owners: a
            .first()
            .and_then(|s| s.parse().ok())
            .unwrap_or_else(|| die("owners gid")),
        writers: opt_u32(a.get(1)),
        readers: opt_u32(a.get(2)),
    }
}

fn sec(s: &str) -> NfsSec {
    match s {
        "sys" => NfsSec::Sys,
        "krb5" => NfsSec::Krb5,
        "krb5i" => NfsSec::Krb5i,
        "krb5p" => NfsSec::Krb5p,
        _ => die("sec"),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first() else {
        die("usage: see the source header")
    };
    let rest = &args[1..];
    let idmap = || {
        CachedIdMap::new(
            Wbinfo::default(),
            Duration::from_secs(60),
            Duration::from_secs(10),
        )
    };
    match cmd.as_str() {
        "apply-root" => {
            let dir = PathBuf::from(&rest[0]);
            let a = apply_root(&SysFs, &dir, &groups(&rest[1..]), None)
                .unwrap_or_else(|e| die(&e.to_string()));
            println!("{a}");
        }
        "check-root" => {
            let dir = PathBuf::from(&rest[0]);
            let f = SysFs.read(&dir).unwrap_or_else(|e| die(&e.to_string()));
            let d = check_root(&f, &groups(&rest[1..]));
            println!(
                "clean={} security_relevant={} {:?}",
                d.is_clean(),
                d.is_security_relevant(),
                d
            );
        }
        "retarget" => {
            let n = retarget_group(
                &SysFs,
                Path::new(&rest[0]),
                rest[1].parse().unwrap_or_else(|_| die("from")),
                opt_u32(rest.get(2)),
            )
            .unwrap_or_else(|e| die(&e.to_string()));
            println!("rewritten={n}");
        }
        "normalize" => {
            let n = omnifile_acl::normalize_tree(&SysFs, Path::new(&rest[0]), &[".omni"])
                .unwrap_or_else(|e| die(&e.to_string()));
            println!("normalized={n}");
        }
        "scan" => {
            let m = idmap();
            let r = scan(&SysFs, &m, Path::new(&rest[0]), &[".omni"])
                .unwrap_or_else(|e| die(&e.to_string()));
            let state_path = rest.get(1).map(PathBuf::from);
            let old: BTreeMap<String, String> = state_path
                .as_ref()
                .and_then(|p| std::fs::read(p).ok())
                .and_then(|b| serde_json::from_slice(&b).ok())
                .unwrap_or_default();
            let mut now = BTreeMap::new();
            let mut changed = Vec::new();
            for f in &r.files {
                let k = f.path.display().to_string();
                if old.get(&k) != Some(&f.acl_sha256) {
                    changed.push(serde_json::json!({"path": k, "acl_sha256": f.acl_sha256}));
                }
                now.insert(k, f.acl_sha256.clone());
            }
            if let Some(p) = state_path {
                std::fs::write(p, serde_json::to_vec(&now).unwrap()).unwrap();
            }
            let out = serde_json::json!({
                "entries_seen": r.entries_seen,
                "distinct_acls": r.acls.len(),
                "findings": r.findings.iter().map(|f| format!("{f:?}")).collect::<Vec<_>>(),
                "changed": changed,
                "acls": r.acl_requests(1, 1000),
            });
            println!("{}", serde_json::to_string_pretty(&out).unwrap());
        }
        "render" => {
            if rest.len() < 10 {
                die("render needs 10 arguments");
            }
            let list = |s: &str| -> Vec<String> {
                if s == "-" {
                    vec![]
                } else {
                    s.split(',').map(str::to_string).collect()
                }
            };
            let protocols: Vec<Protocol> = list(&rest[3])
                .iter()
                .map(|p| match p.as_str() {
                    "nfs3" => Protocol::Nfs3,
                    "nfs4" => Protocol::Nfs4,
                    "smb" => Protocol::Smb,
                    _ => die("protocol"),
                })
                .collect();
            let nfs_sec: Vec<NfsSec> = list(&rest[4]).iter().map(|s| sec(s)).collect();
            let sid =
                |s: &str| (s != "-").then(|| Sid::parse(s).unwrap_or_else(|e| die(&e.to_string())));
            let nfs = protocols.iter().any(|p| *p != Protocol::Smb);
            let cfg = ShareConfig {
                name: rest[0].clone(),
                path: rest[1].clone().into(),
                export_id: rest[2].parse().unwrap_or_else(|_| die("export id")),
                trust_level: TrustLevel::of(nfs, &nfs_sec),
                protocols,
                nfs_sec,
                nfs_clients: list(&rest[5]),
                root_squash: true,
                owners_sid: sid(&rest[6]).unwrap_or_else(|| die("owners sid")),
                writers_sid: sid(&rest[7]),
                readers_sid: sid(&rest[8]),
                restricted: false,
                host_trusted_override: false,
                fsal_policy: match rest[9].as_str() {
                    "wait" => FsalPolicy::Wait,
                    "failfast" => FsalPolicy::FailFast,
                    s => FsalPolicy::WaitQuick {
                        ms: s
                            .strip_prefix("waitquick:")
                            .and_then(|m| m.parse().ok())
                            .unwrap_or_else(|| die("policy")),
                    },
                },
                smb_encrypt: false,
                snapdir: rest.get(10).filter(|s| s.as_str() != "-").map(Into::into),
                smb_prevver: false,
            };
            let mut cfg = cfg;
            if cfg.snapdir.is_some() && cfg.smb_enabled() {
                cfg.smb_prevver = smb_prevver();
            }
            if let Err(e) = cfg.validate() {
                die(&format!("invalid share: {e:?}"));
            }
            if let Some(s) = cfg.render_smb() {
                println!("#--- smb\n{s}");
            }
            if let Some(g) = cfg.render_ganesha() {
                println!("#--- ganesha\n{g}");
            }
        }
        "check-prevver" => {
            use omnifile_acl::smb_module::{check_build, check_prevver};
            let r = if rest.first().map(String::as_str) == Some("--build-only") {
                check_build().map(|b| {
                    format!(
                        "{} built for Samba {}{}",
                        b.host.module_path().display(),
                        b.host.version_full,
                        b.notes
                            .iter()
                            .map(|n| format!("\nnote: {n}"))
                            .collect::<String>()
                    )
                })
            } else {
                check_prevver().map(|ok| ok.to_string())
            };
            match r {
                Ok(msg) => println!("ok: {msg}"),
                Err(e) => {
                    eprintln!("omniacl: FAIL: {e}");
                    std::process::exit(1);
                }
            }
        }
        "resolve-nfs" => {
            let r = Resolver::system(ResolverConfig::default());
            let id = r.resolve_nfs(&NfsRequest {
                uid: rest.first().and_then(|s| s.parse().ok()),
                gid: None,
                sec: rest.get(1).map(|s| sec(s)),
                client_addr: rest.get(2).cloned(),
            });
            println!("{}", serde_json::to_string(&id.requester()).unwrap());
            if let Some(e) = id.error {
                eprintln!("note: {e}");
            }
        }
        "resolve-smb" => {
            let r = Resolver::system(ResolverConfig::default());
            let pid: i32 = rest[0].parse().unwrap_or_else(|_| die("pid"));
            // A pidfd for the target, as FAN_REPORT_PIDFD would supply.
            #[cfg(target_os = "linux")]
            let fd = {
                use std::os::fd::FromRawFd;
                // SAFETY: pidfd_open returns a new fd or -1.
                let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) } as i32;
                (fd >= 0).then(|| unsafe { std::os::fd::OwnedFd::from_raw_fd(fd) })
            };
            #[cfg(not(target_os = "linux"))]
            let fd: Option<std::os::fd::OwnedFd> = None;
            use std::os::fd::AsFd;
            let id = r.resolve_smb(&SmbRequest {
                pid,
                pidfd: fd.as_ref().map(|f| f.as_fd()),
            });
            println!(
                "{} username={:?} method={:?} error={:?}",
                serde_json::to_string(&id.requester()).unwrap(),
                id.username,
                id.method,
                id.error
            );
        }
        _ => die("unknown command"),
    }
}
