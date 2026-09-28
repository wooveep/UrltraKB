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

#[cfg(target_os = "macos")]
fn watch(parent: Option<u32>, timeout: u32) {
    use std::ffi::{c_int, c_long, c_void};
    // Darwin's sys/event.h and sys/time.h layouts. No third-party runtime is needed.
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Event {
        ident: usize,
        filter: i16,
        flags: u16,
        fflags: u32,
        data: isize,
        udata: *mut c_void,
    }
    #[repr(C)]
    struct Timespec {
        seconds: c_long,
        nanoseconds: c_long,
    }
    unsafe extern "C" {
        fn kqueue() -> c_int;
        fn kevent(
            queue: c_int,
            changes: *const Event,
            change_count: c_int,
            events: *mut Event,
            event_count: c_int,
            timeout: *const Timespec,
        ) -> c_int;
        fn close(fd: c_int) -> c_int;
    }
    if let Some(pid) = parent {
        // EVFILT_PROC / NOTE_EXIT watches this process instance. PID reuse cannot
        // hide its exit after registration, unlike polling kill(pid, 0).
        let change = Event {
            ident: pid as usize,
            filter: -5,             // EVFILT_PROC
            flags: 0x0001 | 0x0010, // EV_ADD | EV_ONESHOT
            fflags: 0x8000_0000,    // NOTE_EXIT
            data: 0,
            udata: std::ptr::null_mut(),
        };
        let mut result = change;
        let limit = Timespec {
            seconds: (timeout / 1000).into(),
            nanoseconds: ((timeout % 1000) * 1_000_000).into(),
        };
        // SAFETY: the repr(C) records match Darwin's ABI and remain valid for
        // the blocking call. The descriptor belongs only to this guard thread.
        unsafe {
            let queue = kqueue();
            if queue < 0 {
                std::process::exit(125);
            }
            let count = kevent(queue, &change, 1, &mut result, 1, &limit);
            close(queue);
            // Registration failure (including an already-gone owner) also stops
            // the helper. Only an event-free timeout is reported as a deadline.
            std::process::exit(if count == 0 { 124 } else { 125 });
        }
    }
    thread::sleep(Duration::from_millis(timeout.into()));
    std::process::exit(124);
}
