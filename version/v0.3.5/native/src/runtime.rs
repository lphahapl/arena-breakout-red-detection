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
        Ok(Self { _thread_bound: PhantomData })
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { RoUninitialize() };
    }
}
