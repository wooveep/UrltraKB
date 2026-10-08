//! The isolated Windows child resets PyInstaller's DLL search state, then owns
//! every descendant in a kill-on-close Job. No Qt/main-process global is changed.

#[cfg(windows)]
mod windows {
    use std::{
        ffi::c_void,
        mem,
        os::windows::{io::AsRawHandle, process::CommandExt},
        process::Command,
        ptr,
        time::{Duration, Instant},
    };
    type Handle = *mut c_void;

    #[repr(C)]
    #[derive(Default)]
    struct BasicLimits {
        process_time: i64,
        job_time: i64,
        flags: u32,
        minimum_working_set: usize,
        maximum_working_set: usize,
        active_process_limit: u32,
        affinity: usize,
        priority: u32,
        scheduling: u32,
    }
    #[repr(C)]
    #[derive(Default)]
    struct ExtendedLimits {
        basic: BasicLimits,
        io_counters: [u64; 6],
        process_memory: usize,
        job_memory: usize,
        peak_process_memory: usize,
        peak_job_memory: usize,
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn SetDllDirectoryW(path: *const u16) -> i32;
        fn CreateJobObjectW(attributes: *mut c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(job: Handle, class: i32, data: *const c_void, size: u32) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        fn GetCurrentProcess() -> Handle;
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> Handle;
        fn WaitForSingleObject(handle: Handle, milliseconds: u32) -> u32;
        fn TerminateJobObject(job: Handle, code: u32) -> i32;
    }

    pub fn run() -> Result<i32, Box<dyn std::error::Error>> {
        let args: Vec<_> = std::env::args_os().collect();
        if args.len() < 4 {
            return Err("Expected parent PID, timeout seconds and command".into());
        }
        let parent: u32 = args[1].to_str().ok_or("Invalid PID")?.parse()?;
        let seconds: u64 = args[2].to_str().ok_or("Invalid timeout")?.parse()?;
        if seconds == 0 || seconds > 3635 {
            return Err("Invalid timeout".into());
        }
        // Handles intentionally live until process exit: dropping the job kills
        // this launcher too. Windows closes them on every exit/crash/termination.
        let (parent_handle, job) = unsafe {
            if SetDllDirectoryW(ptr::null()) == 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let parent_handle = OpenProcess(0x00100000, 0, parent); // SYNCHRONIZE
            let job = CreateJobObjectW(ptr::null_mut(), ptr::null());
            if parent_handle.is_null() || job.is_null() {
                return Err(std::io::Error::last_os_error().into());
            }
            let mut limits = ExtendedLimits::default();
            limits.basic.flags = 0x00002000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if SetInformationJobObject(
                job,
                9,
                &limits as *const _ as *const c_void,
                mem::size_of::<ExtendedLimits>() as u32,
            ) == 0
                || AssignProcessToJobObject(job, GetCurrentProcess()) == 0
            {
                return Err(std::io::Error::last_os_error().into());
            }
            (parent_handle, job)
        };
        // Assignment precedes spawn, so Python wrapper, core interpreter and
        // soffice inherit the job with no assign-after-spawn escape window.
        let mut child = Command::new(&args[3])
            .args(&args[4..])
            .creation_flags(0x08000000) // CREATE_NO_WINDOW, also for the Python wrapper.
            .spawn()?;
        let deadline = Instant::now() + Duration::from_secs(seconds);
        loop {
            if let Some(status) = child.try_wait()? {
                return Ok(status.code().unwrap_or(1));
            }
            let parent_state = unsafe { WaitForSingleObject(parent_handle, 0) };
            if parent_state != 258 || Instant::now() >= deadline {
                // WAIT_TIMEOUT = alive
                unsafe {
                    TerminateJobObject(job, 124);
                }
                return Ok(124);
            }
            // Waiting on the owned child avoids busy polling while remaining
            // responsive to parent exit. The Job is the cleanup authority.
            unsafe {
                WaitForSingleObject(child.as_raw_handle(), 50);
            }
        }
    }
}

fn main() {
    #[cfg(windows)]
    match windows::run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("Office launcher: {error}");
            std::process::exit(1);
        }
    }
    #[cfg(not(windows))]
    {
        eprintln!("This Office launcher is for Windows only");
        std::process::exit(1);
    }
}
