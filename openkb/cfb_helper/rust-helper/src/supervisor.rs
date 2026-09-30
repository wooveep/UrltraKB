//! A helper has no children. Its watchdog owns the parent identity until exit.

use std::io;
use std::time::{Duration, Instant};

#[cfg(target_os = "linux")]
struct Parent {
    pid: u32,
    identity: String,
}

#[cfg(target_os = "linux")]
impl Parent {
    fn open(pid: u32, identity: String) -> io::Result<Self> {
        Ok(Self { pid, identity })
    }
    fn alive(&self) -> bool {
        std::fs::read_to_string(format!("/proc/{}/stat", self.pid))
            .ok()
            .and_then(|value| value.rsplit_once(')').map(|(_, rest)| rest.to_owned()))
            .is_some_and(|rest| {
                let fields: Vec<_> = rest.split_whitespace().collect();
                !matches!(fields.first(), Some(&"Z") | Some(&"X") | Some(&"x"))
                    && fields.get(19).is_some_and(|actual| *actual == self.identity)
            })
    }
}

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut std::ffi::c_void;
    fn WaitForSingleObject(handle: *mut std::ffi::c_void, millis: u32) -> u32;
    fn CloseHandle(handle: *mut std::ffi::c_void) -> i32;
}

#[cfg(windows)]
struct Parent(usize);

#[cfg(windows)]
impl Parent {
    fn open(pid: u32, _identity: String) -> io::Result<Self> {
        let handle = unsafe { OpenProcess(0x00100000, 0, pid) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        Ok(Self(handle as usize))
    }
    fn alive(&self) -> bool {
        unsafe { WaitForSingleObject(self.0 as *mut std::ffi::c_void, 0) == 258 }
    }
}

#[cfg(windows)]
impl Drop for Parent {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0 as *mut std::ffi::c_void);
        }
    }
}

#[cfg(not(any(windows, target_os = "linux")))]
compile_error!("openkb-cfb supports Windows and Linux only");

pub fn watch(pid: u32, identity: String, timeout: u64) -> io::Result<()> {
    let parent = Parent::open(pid, identity)?;
    if !parent.alive() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Parent is no longer alive",
        ));
    }
    let deadline = Instant::now()
        .checked_add(Duration::from_millis(timeout))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "Deadline is out of range"))?;
    std::thread::spawn(move || loop {
        if Instant::now() >= deadline || !parent.alive() {
            std::process::exit(124);
        }
        std::thread::sleep(Duration::from_millis(25));
    });
    Ok(())
}
