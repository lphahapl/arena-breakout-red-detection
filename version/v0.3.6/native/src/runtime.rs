use anyhow::Result;
use std::{marker::PhantomData, rc::Rc};
use windows::Win32::System::WinRT::{RoInitialize, RoUninitialize, RO_INIT_MULTITHREADED};

/// A WinRT initialization belongs to its creating thread and must be balanced there.
pub struct Apartment {
    _thread_bound: PhantomData<Rc<()>>,
}

impl Apartment {
    pub fn new() -> Result<Self> {
        unsafe { RoInitialize(RO_INIT_MULTITHREADED)? };
        Ok(Self {
            _thread_bound: PhantomData,
        })
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { RoUninitialize() };
    }
}

// The application holds an MTA for its entire lifetime. Rust's test harness
// instead starts and finishes individual test threads; mirror the application
// lifetime so cached WinRT factories survive between OCR regression tests.
#[cfg(test)]
pub fn keep_test_mta() {
    static READY: std::sync::Once = std::sync::Once::new();
    READY.call_once(|| {
        let (send, receive) = std::sync::mpsc::sync_channel(0);
        std::thread::spawn(move || {
            let _runtime = Apartment::new().expect("test MTA");
            send.send(()).unwrap();
            loop {
                std::thread::park();
            }
        });
        receive.recv().unwrap();
    });
}
