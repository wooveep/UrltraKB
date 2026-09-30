//! Rebuild exactly one selected storage using cfb; never interpret or execute Office content.

use std::env;
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

mod supervisor;

fn relative(root: &Path, entry: &Path) -> io::Result<PathBuf> {
    let suffix = entry
        .strip_prefix(root)
        .map_err(|_| invalid("Storage walk escaped its root"))?;
    Ok(Path::new("/").join(suffix))
}

fn rebuild(input: &Path, storage: &Path, output: &Path, limit: u64) -> io::Result<()> {
    // Common Office writers put ENDOFCHAIN in storage-only sector fields.
    // Let cfb read these compatible inputs; every output is reopened strictly.
    let mut source = cfb::OpenOptions::new().open(input)?;
    if !source.is_storage(storage) {
        return Err(invalid("Selected entry is not a storage"));
    }
    let root = source.entry(storage)?.path().to_owned();
    let entries: Vec<_> = source.walk_storage(&root)?.collect();
    let total =
        entries
            .iter()
            .filter(|entry| entry.is_stream())
            .try_fold(0_u64, |sum, entry| {
                sum.checked_add(entry.len())
                    .ok_or_else(|| invalid("Stream length overflow"))
            })?;
    if total > limit {
        return Err(invalid("Storage exceeds the requested byte budget"));
    }
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(output)?;
    let mut target = cfb::CompoundFile::create_with_version(source.version(), file)?;
    for entry in &entries {
        let path = relative(&root, entry.path())?;
        if entry.is_stream() {
            let mut original = source.open_stream(entry.path())?;
            let mut restored = target.create_new_stream(&path)?;
            let copied = io::copy(
                &mut Read::by_ref(&mut original).take(entry.len()),
                &mut restored,
            )?;
            if copied != entry.len() {
                return Err(invalid("Input stream is incomplete"));
            }
        } else if path != Path::new("/") {
            target.create_storage_all(&path)?;
        }
    }
    // Set metadata last, after stream writes can no longer update storage times.
    for entry in &entries {
        let path = relative(&root, entry.path())?;
        target.set_state_bits(&path, entry.state_bits())?;
        if entry.is_storage() {
            target.set_storage_clsid(&path, *entry.clsid())?;
            if path != Path::new("/") {
                target.set_created_time(&path, entry.created())?;
            } // CFB root creation time remains the specification's zero value.
            target.set_modified_time(&path, entry.modified())?;
        }
    }
    target.flush()?;
    target.into_inner().sync_all()?;
    let mut verified = cfb::OpenOptions::new().strict().open(output)?;
    if verified.walk().count() != entries.len() {
        return Err(invalid("Rebuilt entry count changed"));
    }
    for entry in &entries {
        let path = relative(&root, entry.path())?;
        let restored = verified.entry(&path)?;
        if restored.is_stream() != entry.is_stream()
            || restored.state_bits() != entry.state_bits()
            || restored.clsid() != entry.clsid()
            || (entry.is_stream() && restored.len() != entry.len())
        {
            return Err(invalid("Rebuilt metadata changed"));
        }
        if entry.is_stream() {
            let mut original = source.open_stream(entry.path())?;
            let mut copy = verified.open_stream(&path)?;
            let mut a = [0_u8; 65536];
            let mut b = [0_u8; 65536];
            loop {
                let count = original.read(&mut a)?;
                if count == 0 {
                    break;
                }
                copy.read_exact(&mut b[..count])?;
                if a[..count] != b[..count] {
                    return Err(invalid("Rebuilt stream bytes changed"));
                }
            }
        }
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let args: Vec<_> = env::args().collect();
    if args.len() == 2 && args[1] == "--version" {
        println!("openkb-cfb 1.0.0 cfb 0.15.0");
        return Ok(());
    }
    if args.len() != 8 {
        return Err(invalid(
            "Expected input, storage, output, bytes, milliseconds, parent pid and identity",
        ));
    }
    let limit = args[4]
        .parse::<u64>()
        .map_err(|_| invalid("Invalid byte limit"))?;
    let timeout = args[5]
        .parse::<u64>()
        .map_err(|_| invalid("Invalid deadline"))?;
    let pid = args[6]
        .parse::<u32>()
        .map_err(|_| invalid("Invalid parent pid"))?;
    let identity = args[7].clone();
    if limit == 0 || timeout == 0 || pid == 0 {
        return Err(invalid("Invalid supervision boundary"));
    }
    supervisor::watch(pid, identity, timeout)?;
    rebuild(
        Path::new(&args[1]),
        Path::new(&args[2]),
        Path::new(&args[3]),
        limit,
    )?;
    io::stdout().write_all(b"OK\n")?;
    Ok(())
}
