use anyhow::{bail, Result};
use image::RgbImage;
use serde::Serialize;
use windows::core::BOOL;
use windows::Win32::{
    Foundation::{HWND, LPARAM, POINT, RECT},
    Graphics::Gdi::*,
    UI::{
        HiDpi::{SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2},
        WindowsAndMessaging::*,
    },
};

#[derive(Serialize, Clone)]
pub struct Window {
    pub title: String,
    pub w: i32,
    pub h: i32,
    #[serde(skip)]
    pub hwnd: isize,
}
pub fn dpi() {
    unsafe {
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
}
unsafe extern "system" fn enumerate(hwnd: HWND, data: LPARAM) -> BOOL {
    let windows = &mut *(data.0 as *mut Vec<Window>);
    if !IsWindowVisible(hwnd).as_bool() {
        return BOOL(1);
    }
    let n = GetWindowTextLengthW(hwnd);
    if n <= 0 {
        return BOOL(1);
    }
    let mut text = vec![0u16; n as usize + 1];
    GetWindowTextW(hwnd, &mut text);
    let mut rect = RECT::default();
    if GetClientRect(hwnd, &mut rect).is_err() {
        return BOOL(1);
    }
    let (w, h) = (rect.right - rect.left, rect.bottom - rect.top);
    if w >= 400 && h >= 400 {
        windows.push(Window {
            title: String::from_utf16_lossy(&text[..n as usize]),
            w,
            h,
            hwnd: hwnd.0 as isize,
        });
    }
    BOOL(1)
}
pub fn windows() -> Vec<Window> {
    let mut list: Vec<Window> = vec![];
    unsafe {
        let _ = EnumWindows(Some(enumerate), LPARAM(&mut list as *mut _ as isize));
    }
    list.sort_by_key(|w| std::cmp::Reverse(w.w as i64 * w.h as i64));
    list
}
pub fn find(title: &str) -> Result<Window> {
    windows()
        .into_iter()
        .find(|w| w.title == title)
        .or_else(|| windows().into_iter().find(|w| w.title.contains(title)))
        .ok_or_else(|| anyhow::anyhow!("找不到目标窗口：{title}"))
}
pub fn region(window: &Window, roi: Option<[f64; 4]>) -> Result<[i32; 4]> {
    unsafe {
        let hwnd = HWND(window.hwnd as *mut _);
        let mut r = RECT::default();
        GetClientRect(hwnd, &mut r)?;
        let (w, h) = (r.right - r.left, r.bottom - r.top);
        if w <= 0 || h <= 0 {
            bail!("目标窗口已最小化");
        }
        let mut p = POINT::default();
        if !ClientToScreen(hwnd, &mut p).as_bool() {
            bail!("无法定位目标窗口");
        }
        let mut rect = [p.x, p.y, w, h];
        if let Some(a) = roi {
            rect = [
                p.x + (w as f64 * a[0]).round() as i32,
                p.y + (h as f64 * a[1]).round() as i32,
                (w as f64 * a[2]).round().max(1.) as i32,
                (h as f64 * a[3]).round().max(1.) as i32,
            ];
        }
        Ok(rect)
    }
}
pub fn visible(window: &Window, rect: [i32; 4]) -> bool {
    unsafe {
        let target = HWND(window.hwnd as *mut _);
        let mut hits = 0;
        for y in 0..5 {
            for x in 0..5 {
                let p = POINT {
                    x: rect[0] + (rect[2] as f64 * (x as f64 + 0.5) / 5.) as i32,
                    y: rect[1] + (rect[3] as f64 * (y as f64 + 0.5) / 5.) as i32,
                };
                if GetAncestor(WindowFromPoint(p), GA_ROOT) == target {
                    hits += 1;
                }
            }
        }
        hits >= 15
    }
}
struct Surface {
    screen: HDC,
    dc: HDC,
    bitmap: HBITMAP,
    previous: HGDIOBJ,
}
impl Drop for Surface {
    fn drop(&mut self) {
        unsafe {
            if !self.previous.is_invalid() {
                SelectObject(self.dc, self.previous);
            }
            if !self.bitmap.is_invalid() {
                let _ = DeleteObject(self.bitmap.into());
            }
            if !self.dc.is_invalid() {
                let _ = DeleteDC(self.dc);
            }
            if !self.screen.is_invalid() {
                ReleaseDC(None, self.screen);
            }
        }
    }
}
pub fn grab(rect: [i32; 4]) -> Result<RgbImage> {
    unsafe {
        let w = rect[2];
        let h = rect[3];
        if w <= 0 || h <= 0 || w > 16384 || h > 16384 {
            bail!("截图范围无效");
        }
        let mut s = Surface {
            screen: GetDC(None),
            dc: HDC::default(),
            bitmap: HBITMAP::default(),
            previous: HGDIOBJ::default(),
        };
        if s.screen.is_invalid() {
            bail!("无法创建截图 DC");
        }
        s.dc = CreateCompatibleDC(Some(s.screen));
        if s.dc.is_invalid() {
            bail!("无法创建截图缓冲");
        }
        let info = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: -h,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut pixels = std::ptr::null_mut();
        s.bitmap = CreateDIBSection(Some(s.screen), &info, DIB_RGB_COLORS, &mut pixels, None, 0)?;
        s.previous = SelectObject(s.dc, s.bitmap.into());
        BitBlt(
            s.dc,
            0,
            0,
            w,
            h,
            Some(s.screen),
            rect[0],
            rect[1],
            SRCCOPY | CAPTUREBLT,
        )?;
        let data = std::slice::from_raw_parts(pixels as *const u8, w as usize * h as usize * 4);
        let mut rgb = vec![0; w as usize * h as usize * 3];
        for (a, b) in data.chunks_exact(4).zip(rgb.chunks_exact_mut(3)) {
            b.copy_from_slice(&[a[2], a[1], a[0]]);
        }
        Ok(RgbImage::from_raw(w as u32, h as u32, rgb).unwrap())
    }
}
