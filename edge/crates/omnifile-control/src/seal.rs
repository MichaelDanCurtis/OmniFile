//! The edge's private keys at rest (M6, "TPM-held keys";
//! docs/design/setup-wizard.md, "Keys").
//!
//! With [`KeyProtection::File`] a key is a 0600 file, as in M1. With any other
//! mode it is sealed with systemd credentials (`systemd-creds encrypt`):
//! - `auto` binds it to the TPM2 when the box has one, and to the host key
//!   (`/var/lib/systemd/credential.secret`) as well;
//! - `tpm2` binds it to the TPM2 alone, and fails on a box without one;
//! - `host` uses the host key alone.
//!
//! A sealed key, copied off the disk, is of no use without that box's TPM.
//! It is bound to no PCRs, so firmware updates don't lock it out; a cleared
//! or replaced TPM does, and the filer is then enrolled again.
//! The daemon decrypts it into memory when it loads the identity, and never
//! writes the plaintext. The credential's name is bound in (a sealed identity
//! won't decrypt as anything else).
//!
//! The mode is process-wide: the daemon sets it once at startup from
//! `[service] key_protection`. A plain file found while sealing is on is
//! sealed in place the first time it is read.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU8, Ordering};

use crate::error::{ControlError, Result};
use crate::util::write_atomic;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyProtection {
    File,
    Host,
    Auto,
    Tpm2,
}

impl KeyProtection {
    pub fn parse(s: &str) -> Result<Self> {
        match s {
            "file" => Ok(Self::File),
            "host" => Ok(Self::Host),
            "auto" => Ok(Self::Auto),
            "tpm2" => Ok(Self::Tpm2),
            _ => Err(ControlError::Invalid(format!(
                "key_protection {s:?}: use file, host, auto or tpm2"
            ))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::File => "file",
            Self::Host => "host",
            Self::Auto => "auto",
            Self::Tpm2 => "tpm2",
        }
    }
}

static MODE: AtomicU8 = AtomicU8::new(0);

pub fn set_key_protection(p: KeyProtection) {
    MODE.store(
        match p {
            KeyProtection::File => 0,
            KeyProtection::Host => 1,
            KeyProtection::Auto => 2,
            KeyProtection::Tpm2 => 3,
        },
        Ordering::SeqCst,
    );
}

pub fn key_protection() -> KeyProtection {
    match MODE.load(Ordering::SeqCst) {
        1 => KeyProtection::Host,
        2 => KeyProtection::Auto,
        3 => KeyProtection::Tpm2,
        _ => KeyProtection::File,
    }
}

/// `systemd-creds`, or the binary `OMNIFILE_SYSTEMD_CREDS` names (tests).
fn creds_bin() -> String {
    std::env::var("OMNIFILE_SYSTEMD_CREDS").unwrap_or_else(|_| "systemd-creds".into())
}

fn creds(args: &[&str], input: &[u8]) -> Result<Vec<u8>> {
    let bin = creds_bin();
    let mut child = Command::new(&bin)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| ControlError::Identity(format!("{bin}: {e}")))?;
    child.stdin.take().expect("piped").write_all(input)?;
    let out = child.wait_with_output()?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        let last = err.lines().rfind(|l| !l.trim().is_empty()).unwrap_or("");
        return Err(ControlError::Identity(format!("{bin} {}: {last}", args[0])));
    }
    Ok(out.stdout)
}

/// Seal `plain` under `name` with the current mode.
pub fn seal(name: &str, plain: &[u8]) -> Result<Vec<u8>> {
    let with = match key_protection() {
        KeyProtection::File => {
            return Err(ControlError::Identity(
                "sealing is off (key_protection = file)".into(),
            ));
        }
        p => p.as_str(),
    };
    // No PCRs: the key is bound to this TPM, not to the firmware's state,
    // so a firmware or Secure Boot database update doesn't lock it out.
    creds(
        &[
            "encrypt",
            &format!("--name={name}"),
            &format!("--with-key={with}"),
            "--tpm2-pcrs=",
            // Nor to a signed PCR policy, should the image ever ship a
            // PCR-signing public key: `auto` would pick that up by itself.
            "--tpm2-public-key=",
            "-",
            "-",
        ],
        plain,
    )
}

/// Open a credential sealed under `name`.
pub fn unseal(name: &str, sealed: &[u8]) -> Result<Vec<u8>> {
    creds(&["decrypt", &format!("--name={name}"), "-", "-"], sealed)
}

/// Seals and opens a probe with the current mode (`omnifile-edge --check`),
/// so a box that can't seal (`tpm2` without a TPM, a host key the daemon
/// can't read) fails at start, saying why, not at its first renewal.
pub fn probe() -> Result<()> {
    if key_protection() == KeyProtection::File {
        return Ok(());
    }
    let plain = b"omnifile key-protection probe";
    if unseal("omnifile-probe", &seal("omnifile-probe", plain)?)? != plain {
        return Err(ControlError::Identity(
            "systemd-creds: a sealed probe did not open to itself".into(),
        ));
    }
    Ok(())
}

/// Where the sealed form of `plain_path` lives: the same name, `.cred`.
pub fn sealed_path(plain_path: &Path) -> PathBuf {
    plain_path.with_extension("cred")
}

/// Writes a secret: sealed (`<name>.cred`) unless the mode is `file`. The
/// other form, if any, is removed, so one file holds the current secret.
pub fn write_secret(plain_path: &Path, name: &str, data: &[u8]) -> Result<()> {
    let sealed = sealed_path(plain_path);
    if key_protection() == KeyProtection::File {
        write_atomic(plain_path, data, 0o600)?;
        remove(&sealed)
    } else {
        write_atomic(&sealed, &seal(name, data)?, 0o600)?;
        remove(plain_path)
    }
}

/// A secret read back, and whether it was found in the clear while sealing
/// is on: such a key was on disk unsealed (its blocks, backups and snapshots
/// may still hold it), so its owner replaces it ([`crate::Identity`]
/// renews at once).
pub struct Read {
    pub data: Vec<u8>,
    pub was_plain: bool,
}

/// Reads a secret in whichever form is there; `None` when neither is.
/// While sealing is on, a plain file is sealed in place, and a plain file
/// left beside a sealed one (a crash between the two writes) is removed.
pub fn read_secret_full(plain_path: &Path, name: &str) -> Result<Option<Read>> {
    let sealed = sealed_path(plain_path);
    let sealing = key_protection() != KeyProtection::File;
    if sealed.exists() {
        let data = unseal(name, &std::fs::read(&sealed)?)?;
        let was_plain = sealing && plain_path.exists();
        if was_plain {
            remove(plain_path)?;
        }
        return Ok(Some(Read { data, was_plain }));
    }
    match std::fs::read(plain_path) {
        Ok(data) => {
            if sealing {
                write_secret(plain_path, name, &data)?;
            }
            Ok(Some(Read {
                data,
                was_plain: sealing,
            }))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

/// [`read_secret_full`], the data only.
pub fn read_secret(plain_path: &Path, name: &str) -> Result<Option<Vec<u8>>> {
    Ok(read_secret_full(plain_path, name)?.map(|r| r.data))
}

/// Whether a secret exists in either form.
pub fn secret_exists(plain_path: &Path) -> bool {
    plain_path.exists() || sealed_path(plain_path).exists()
}

/// Removes a secret in both forms.
pub fn remove_secret(plain_path: &Path) {
    let _ = std::fs::remove_file(plain_path);
    let _ = std::fs::remove_file(sealed_path(plain_path));
}

fn remove(p: &Path) -> Result<()> {
    match std::fs::remove_file(p) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.into()),
        _ => Ok(()),
    }
}
