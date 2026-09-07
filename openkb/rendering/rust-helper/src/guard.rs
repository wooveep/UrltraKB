//! Stop this read-only helper if its owner exits, even during native rendering.
use std::{env, thread, time::Duration};

pub fn install() {
    let parent = env::var("OPENKB_RENDER_PARENT_PID")
        .ok()
        .and_then(|value| value.parse::<u32>().ok());
    let timeout = env::var("OPENKB_RENDER_TIMEOUT_MS")
        .ok()
        .and_then(|value| value.parse::<u32>().ok())
        .unwrap_or(60_000)
        .clamp(100, 60_000);
    thread::spawn(move || watch(parent, timeout));
}

#[cfg(target_os = "linux")]
fn watch(parent: Option<u32>, timeout: u32) {
    let deadline = std::time::Instant::now() + Duration::from_millis(timeout.into());
    let expected = env::var("OPENKB_RENDER_PARENT_START").ok();
    loop {
        if let Some(pid) = parent {
            let alive = std::fs::read_to_string(format!("/proc/{pid}/stat"))
                .ok()
                .and_then(|stat| {
                    let fields: Vec<_> = stat.get(stat.rfind(')')? + 2..)?.split(' ').collect();
                    Some(
                        fields.first()? != &"Z"
                            && expected
                                .as_deref()
                                .is_none_or(|start| fields.get(19).copied() == Some(start)),
                    )
                })
                .unwrap_or(false);
            if !alive {
                std::process::exit(125);
            }
        }
        if std::time::Instant::now() >= deadline {
            std::process::exit(124);
        }
        thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(windows)]
fn watch(parent: Option<u32>, timeout: u32) {
    use std::ffi::c_void;
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn WaitForSingleObject(handle: *mut c_void, milliseconds: u32) -> u32;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }
    if let Some(pid) = parent {
        // SYNCHRONIZE grants only waiting for process termination. The handle
        // refers to this process instance, so PID reuse cannot mask owner loss.
        unsafe {
            let handle = OpenProcess(0x0010_0000, 0, pid);
            if handle.is_null() {
                std::process::exit(125);
            }
            let outcome = WaitForSingleObject(handle, timeout);
            CloseHandle(handle);
            std::process::exit(if outcome == 0x0000_0102 { 124 } else { 125 });
        }
    }
    thread::sleep(Duration::from_millis(timeout.into()));
    std::process::exit(124);
}
