use anyhow::Result;
use image::{imageops, RgbImage};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    hash::{Hash, Hasher},
    time::Instant,
};
use windows::{
    core::{Interface, HSTRING},
    Globalization::Language,
    Graphics::Imaging::{BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap},
    Media::Ocr::OcrEngine,
    Storage::Streams::Buffer,
    Win32::System::WinRT::IBufferByteAccess,
};

pub type Rect = [i32; 4];
#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub right_frac: f64,
    pub work_region: Option<[f64; 4]>,
    pub container_keyword: String,
    pub ocr_interval: f64,
    pub ocr_full_interval: f64,
    pub red_sat_min: f64,
    pub red_val_min: u8,
    pub red_gb_ratio_max: f64,
    pub red_highlight_val_min: f64,
    pub red_highlight_sat_min: f64,
    pub red_highlight_gb_max: f64,
    pub red_hue_max: f64,
    pub red_hue_wrap_min: f64,
    pub red_close_win: usize,
    pub red_extent_min: f64,
    pub red_area_frac_min: f64,
    pub red_confirm_rounds: usize,
    pub red_confirm_window: usize,
    pub tpl_match_min: f64,
    pub tpl_gone_max: f64,
    pub tpl_gray_max_rounds: usize,
    pub interval: f64,
    pub session_interval: f64,
    pub miss_need: usize,
    pub red_exclude_words: Vec<String>,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            right_frac: 0.4,
            work_region: None,
            container_keyword: "保险箱".into(),
            ocr_interval: 0.5,
            ocr_full_interval: 1.0,
            red_sat_min: 65.,
            red_val_min: 60,
            red_gb_ratio_max: 0.28,
            red_highlight_val_min: 180.,
            red_highlight_sat_min: 25.,
            red_highlight_gb_max: 0.08,
            red_hue_max: 9.,
            red_hue_wrap_min: 170.,
            red_close_win: 25,
            red_extent_min: 0.30,
            red_area_frac_min: 0.0025,
            red_confirm_rounds: 2,
            red_confirm_window: 5,
            tpl_match_min: 0.65,
            tpl_gone_max: 0.35,
            tpl_gray_max_rounds: 5,
            interval: 0.1,
            session_interval: 0.01,
            miss_need: 3,
            red_exclude_words: crate::keyword_filter::defaults(),
        }
    }
}
pub fn now() -> String {
    chrono::Utc::now()
        .with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).unwrap())
        .to_rfc3339_opts(chrono::SecondsFormat::Secs, false)
}
pub fn crop(image: &RgbImage, r: Rect) -> RgbImage {
    let x = r[0].clamp(0, image.width() as i32 - 1) as u32;
    let y = r[1].clamp(0, image.height() as i32 - 1) as u32;
    let w = (r[2].max(1) as u32).min(image.width() - x);
    let h = (r[3].max(1) as u32).min(image.height() - y);
    imageops::crop_imm(image, x, y, w, h).to_image()
}
pub fn work_rect(cfg: &Config, w: i32, h: i32) -> Rect {
    if let Some(r) = cfg.work_region {
        [
            (r[0] * w as f64).round() as i32,
            (r[1] * h as f64).round() as i32,
            (r[2] * w as f64).round() as i32,
            (r[3] * h as f64).round() as i32,
        ]
    } else {
        let x = (w as f64 * (1. - cfg.right_frac)).round() as i32;
        [x, 0, w - x, h]
    }
}
#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct Line {
    pub text: String,
    pub bbox: Rect,
}
pub struct Engine {
    inner: OcrEngine,
    // Fields drop in declaration order: release the COM object before uninitializing.
    _apartment: crate::runtime::Apartment,
}
impl Engine {
    pub fn new() -> Result<Self> {
        let apartment = crate::runtime::Apartment::new()?;
        let lang = Language::CreateLanguage(&HSTRING::from("zh-Hans-CN"))?;
        Ok(Self {
            inner: OcrEngine::TryCreateFromLanguage(&lang)?,
            _apartment: apartment,
        })
    }
    pub fn recognize(&self, image: &RgbImage, scale: f64) -> Result<Vec<Line>> {
        let scaled;
        let im = if scale == 1. {
            image
        } else {
            scaled = imageops::resize(
                image,
                (image.width() as f64 * scale).round() as u32,
                (image.height() as f64 * scale).round() as u32,
                imageops::FilterType::CatmullRom,
            );
            &scaled
        };
        let len = im.width() * im.height() * 4;
        let buffer = Buffer::Create(len)?;
        buffer.SetLength(len)?;
        let access: IBufferByteAccess = buffer.cast()?;
        // The buffer owns exactly len writable bytes. Copy before OCR starts.
        let pixels = unsafe { std::slice::from_raw_parts_mut(access.Buffer()?, len as usize) };
        for (source, target) in im.pixels().zip(pixels.chunks_exact_mut(4)) {
            target.copy_from_slice(&[source[2], source[1], source[0], 255]);
        }
        let bitmap = SoftwareBitmap::CreateCopyWithAlphaFromBuffer(
            &buffer,
            BitmapPixelFormat::Bgra8,
            im.width() as i32,
            im.height() as i32,
            BitmapAlphaMode::Ignore,
        )?;
        let result = self.inner.RecognizeAsync(&bitmap)?.join()?;
        let mut lines = vec![];
        for line in result.Lines()? {
            let mut x = f64::INFINITY;
            let mut y = f64::INFINITY;
            let mut right: f64 = 0.;
            let mut bottom: f64 = 0.;
            for word in line.Words()? {
                let r = word.BoundingRect()?;
                x = x.min(r.X as f64);
                y = y.min(r.Y as f64);
                right = right.max((r.X + r.Width) as f64);
                bottom = bottom.max((r.Y + r.Height) as f64);
            }
            if !x.is_finite() {
                continue;
            }
            let text = line
                .Text()?
                .to_string()
                .chars()
                .filter(|c| !c.is_whitespace())
                .collect();
            lines.push(Line {
                text,
                bbox: [
                    (x / scale).round() as i32,
                    (y / scale).round() as i32,
                    ((right - x) / scale).round() as i32,
                    ((bottom - y) / scale).round() as i32,
                ],
            });
        }
        Ok(lines)
    }
}

#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct Blob {
    pub bbox: Rect,
    pub area: usize,
    pub extent: f64,
    pub aspect: f64,
    pub accepted: bool,
    pub reject: Vec<String>,
    pub ocr_text: String,
    pub ocr_name: String,
    pub confirm_votes: usize,
}
fn clear(mask: &mut [u8], w: usize, h: usize, r: Rect) {
    for y in r[1].max(0) as usize..(r[1] + r[3]).clamp(0, h as i32) as usize {
        for x in r[0].max(0) as usize..(r[0] + r[2]).clamp(0, w as i32) as usize {
            mask[y * w + x] = 0;
        }
    }
}
fn morph(input: &[u8], w: usize, h: usize, radius: usize, dilate: bool) -> Vec<u8> {
    let mut horizontal = vec![0; w * h];
    let mut out = vec![0; w * h];
    let mut prefix = vec![0usize; w.max(h) + 1];
    for y in 0..h {
        prefix[0] = 0;
        for x in 0..w {
            prefix[x + 1] = prefix[x] + input[y * w + x] as usize;
        }
        for x in 0..w {
            let a = x.saturating_sub(radius);
            let b = (x + radius + 1).min(w);
            let n = prefix[b] - prefix[a];
            horizontal[y * w + x] = if dilate {
                (n > 0) as u8
            } else {
                (n == b - a) as u8
            };
        }
    }
    for x in 0..w {
        prefix[0] = 0;
        for y in 0..h {
            prefix[y + 1] = prefix[y] + horizontal[y * w + x] as usize;
        }
        for y in 0..h {
            let a = y.saturating_sub(radius);
            let b = (y + radius + 1).min(h);
            let n = prefix[b] - prefix[a];
            out[y * w + x] = if dilate {
                (n > 0) as u8
            } else {
                (n == b - a) as u8
            };
        }
    }
    out
}
fn find_red_cells(image: &RgbImage, cfg: &Config, exclude: Rect, cell: f64) -> Vec<Blob> {
    let (w, h) = (image.width() as usize, image.height() as usize);
    let mut mask = vec![0u8; w * h];
    for (i, p) in image.pixels().enumerate() {
        let (r, g, b) = (p[0] as f64, p[1] as f64, p[2] as f64);
        let max = r.max(g).max(b);
        let min = r.min(g).min(b);
        let d = max - min;
        if d == 0. || max < cfg.red_val_min as f64 {
            continue;
        }
        let hue = if max == r {
            30. * ((g - b) / d).rem_euclid(6.)
        } else if max == g {
            30. * ((b - r) / d + 2.)
        } else {
            30. * ((r - g) / d + 4.)
        };
        let hue = hue.round().rem_euclid(180.);
        let sat = (255. * d / max).round();
        let gb = (g - b).abs() / g.max(b).max(1.);
        let normal = (hue <= cfg.red_hue_max || hue >= cfg.red_hue_wrap_min)
            && sat >= cfg.red_sat_min
            && gb <= cfg.red_gb_ratio_max
            // Dark orange/brown rarity backgrounds overlap the broad hue range.
            // Preserve warm highlights; shadowed red backgrounds have G close to B.
            && (max >= 100. || hue <= 6. || hue >= 174.);
        let highlight = (hue <= cfg.red_hue_max.min(6.) || hue >= cfg.red_hue_wrap_min.max(174.))
            && sat >= cfg.red_highlight_sat_min
            && max >= cfg.red_highlight_val_min
            && gb <= cfg.red_highlight_gb_max;
        if normal || highlight {
            mask[i] = 1;
        }
    }
    clear(&mut mask, w, h, exclude);
    let radius = cfg.red_close_win / 2;
    if radius > 0 {
        mask = morph(&morph(&mask, w, h, radius, true), w, h, radius, false);
    }
    clear(&mut mask, w, h, exclude);
    let mut visited = vec![false; w * h];
    let mut queue = vec![];
    let mut blobs = vec![];
    for i in 0..w * h {
        if mask[i] == 0 || visited[i] {
            continue;
        }
        queue.clear();
        queue.push(i);
        visited[i] = true;
        let (mut left, mut right, mut top, mut bottom) = (i % w, i % w, i / w, i / w);
        let mut head = 0;
        while head < queue.len() {
            let pos = queue[head];
            head += 1;
            let (x, y) = (pos % w, pos / w);
            left = left.min(x);
            right = right.max(x);
            top = top.min(y);
            bottom = bottom.max(y);
            for ny in y.saturating_sub(1)..=(y + 1).min(h - 1) {
                for nx in x.saturating_sub(1)..=(x + 1).min(w - 1) {
                    let n = ny * w + nx;
                    if mask[n] > 0 && !visited[n] {
                        visited[n] = true;
                        queue.push(n);
                    }
                }
            }
        }
        let (bw, bh) = (right - left + 1, bottom - top + 1);
        let area = queue.len();
        let extent = area as f64 / (bw * bh) as f64;
        let aspect = bw as f64 / bh as f64;
        let mut reject = vec![];
        if area as f64 / ((w * h) as f64) < cfg.red_area_frac_min {
            reject.push("area_frac".into());
        }
        if extent < cfg.red_extent_min {
            reject.push("extent".into());
        }
        if aspect <= 0.35 || aspect >= 2.8 {
            reject.push("aspect".into());
        }
        let cell = cell.max(1.);
        if (bw as f64) < cell
            || (bh as f64) < cell
            || ((bw * bh) as f64) < cell * cell * 2.
            || bw as f64 > cell * 4.
            || bh as f64 > cell * 4.
        {
            reject.push("size".into());
        }
        blobs.push(Blob {
            bbox: [left as i32, top as i32, bw as i32, bh as i32],
            area,
            extent,
            aspect,
            accepted: reject.is_empty(),
            reject,
            ocr_text: String::new(),
            ocr_name: String::new(),
            confirm_votes: 0,
        });
    }
    blobs.sort_by_key(|b| (!b.accepted, std::cmp::Reverse(b.area)));
    blobs
}
fn safe_title(text: &str, keyword: &str) -> bool {
    text.ends_with(keyword)
        && text.chars().count() <= 8
        && ![
            "等待", "出现", "打开", "检测", "记录", "次数", "搜索", "寻找",
        ]
        .iter()
        .any(|word| text.contains(word))
}
// Measure the actual inventory lattice instead of inferring a slot from OCR height.
// Three equally spaced long neutral dividers must begin near the title's left edge.
fn grid_cell(work: &RgbImage, anchor: Rect) -> Option<f64> {
    let panel = panel_rect(anchor);
    let left = panel[0].max(3) as usize;
    let right = (panel[0] + panel[2]).min(work.width() as i32 - 3).max(0) as usize;
    let top = (anchor[1] + anchor[3] + anchor[3] / 2).max(0) as usize;
    let bottom = (panel[1] + panel[3]).min(work.height() as i32).max(0) as usize;
    let mut columns: Vec<i32> = vec![];
    let mut group: Vec<i32> = vec![];
    for x in left..right {
        let count = (top..bottom)
            .filter(|&y| {
                let p = work.get_pixel(x as u32, y as u32);
                let max = *p.0.iter().max().unwrap() as f64;
                let min = *p.0.iter().min().unwrap() as f64;
                let v = gray(p);
                max - min <= max * 0.40
                    && v >= 30.
                    && (v - gray(work.get_pixel((x - 3) as u32, y as u32))).abs() >= 8.
                    && (v - gray(work.get_pixel((x + 3) as u32, y as u32))).abs() >= 8.
            })
            .count();
        if count as f64 >= anchor[3] as f64 * 4.5 {
            if group.last().is_some_and(|last| x as i32 - last > 3) {
                columns.push(group.iter().sum::<i32>() / group.len() as i32);
                group.clear();
            }
            group.push(x as i32);
        }
    }
    if !group.is_empty() {
        columns.push(group.iter().sum::<i32>() / group.len() as i32);
    }
    for &first in &columns {
        if (first - anchor[0]).abs() > anchor[3] {
            continue;
        }
        for &second in &columns {
            let gap = second - first;
            if gap < anchor[3] * 2 || gap > anchor[3] * 6 {
                continue;
            }
            if let Some(&third) = columns.iter().find(|&&x| (x - second - gap).abs() <= 3) {
                return Some((third - first) as f64 / 2.);
            }
        }
    }
    None
}
pub fn iou(a: Rect, b: Rect) -> f64 {
    let overlap = (a[0] + a[2])
        .min(b[0] + b[2])
        .saturating_sub(a[0].max(b[0]))
        .max(0) as f64
        * (a[1] + a[3])
            .min(b[1] + b[3])
            .saturating_sub(a[1].max(b[1]))
            .max(0) as f64;
    let union = (a[2] * a[3] + b[2] * b[3]) as f64 - overlap;
    if union > 0. {
        overlap / union
    } else {
        0.
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Session {
    pub start: String,
    pub end: Option<String>,
    pub end_reason: Option<String>,
    pub ocr_text: String,
    pub name_bbox: Rect,
    pub panel_rect: Rect,
    pub red: bool,
    pub red_blobs: Vec<Blob>,
    pub observations: usize,
}
pub struct Tracker {
    pub cfg: Config,
    pub engine: Engine,
    pub session: Option<Session>,
    pub total: usize,
    pub streak: usize,
    pub misses: usize,
    pub score: f64,
    pub ocr_calls: usize,
    pub ocr_ms_total: f64,
    pub item_ocr_calls: usize,
    pub item_ms_total: f64,
    pub text_cache_hits: usize,
    last_ocr: Option<Instant>,
    last_full: Option<Instant>,
    idle_anchor: Option<Rect>,
    shape: (u32, u32),
    template: Option<(RgbImage, Rect)>,
    gray: usize,
    votes: Vec<Vec<Rect>>,
    cache: HashMap<u64, (String, String)>,
    pub candidates: Vec<Blob>,
    measured_cell: Option<(Rect, f64)>,
}
pub struct Step {
    pub kind: Option<String>,
    pub session: Option<Session>,
    pub visible: bool,
    pub reds: Vec<Blob>,
}
impl Tracker {
    pub fn new(cfg: Config) -> Result<Self> {
        Ok(Self {
            cfg,
            engine: Engine::new()?,
            session: None,
            total: 0,
            streak: 0,
            misses: 0,
            score: 0.,
            ocr_calls: 0,
            ocr_ms_total: 0.,
            item_ocr_calls: 0,
            item_ms_total: 0.,
            text_cache_hits: 0,
            last_ocr: None,
            last_full: None,
            idle_anchor: None,
            shape: (0, 0),
            template: None,
            gray: 0,
            votes: vec![],
            cache: HashMap::new(),
            candidates: vec![],
            measured_cell: None,
        })
    }
    fn title(&mut self, work: &RgbImage) -> Result<Option<Line>> {
        if self.shape != (work.width(), work.height()) {
            self.idle_anchor = None;
            self.shape = (work.width(), work.height());
        }
        let full = self.idle_anchor.is_none()
            || self.last_full.map_or(true, |t| {
                t.elapsed().as_secs_f64() >= self.cfg.ocr_full_interval
            });
        let iv = if self.idle_anchor.is_some() {
            self.cfg.ocr_interval.min(0.2)
        } else {
            self.cfg.ocr_interval
        };
        if self
            .last_ocr
            .is_some_and(|t| t.elapsed().as_secs_f64() < iv)
        {
            return Ok(None);
        }
        self.last_ocr = Some(Instant::now());
        let start = Instant::now();
        let rect = if full {
            self.last_full = Some(Instant::now());
            [0, 0, work.width() as i32, work.height() as i32]
        } else {
            let n = self.idle_anchor.unwrap();
            let px = 100.max(n[2]);
            let py = 35.max((n[3] as f64 * 2.5) as i32);
            [
                (n[0] - px).max(0),
                (n[1] - py).max(0),
                n[2] + 2 * px,
                n[3] + 2 * py,
            ]
        };
        let sub = crop(work, rect);
        let mut hit = None;
        for scale in [1., 1.5] {
            for mut line in self.engine.recognize(&sub, scale)? {
                if safe_title(&line.text, &self.cfg.container_keyword) {
                    line.bbox[0] += rect[0];
                    line.bbox[1] += rect[1];
                    let Some(cell) = grid_cell(work, line.bbox) else {
                        continue;
                    };
                    self.measured_cell = Some((line.bbox, cell));
                    hit = Some(line);
                    break;
                }
            }
            if hit.is_some() {
                break;
            }
        }
        self.ocr_calls += 1;
        self.ocr_ms_total += start.elapsed().as_secs_f64() * 1000.;
        Ok(hit)
    }
    pub fn scan(&mut self, work: &RgbImage, anchor: Rect) -> Result<Vec<Blob>> {
        let panel = panel_rect(anchor);
        let sub = crop(work, panel);
        let exclude = [
            (anchor[0] - (anchor[2] as f64 * 0.6) as i32).max(0) - panel[0],
            (anchor[1] - (anchor[3] as f64 * 0.8) as i32).max(0) - panel[1],
            anchor[2] + 2 * (anchor[2] as f64 * 0.6) as i32,
            anchor[3] + 2 * (anchor[3] as f64 * 0.8) as i32,
        ];
        let cell = match self.measured_cell {
            Some((old, cell)) if old == anchor => cell,
            _ => {
                let cell = grid_cell(work, anchor).unwrap_or(anchor[3] as f64 * 4.);
                self.measured_cell = Some((anchor, cell));
                cell
            }
        };
        let mut blobs = find_red_cells(&sub, &self.cfg, exclude, cell);
        self.votes.push(
            blobs
                .iter()
                .filter(|b| b.accepted)
                .map(|b| {
                    let mut r = b.bbox;
                    r[0] += panel[0];
                    r[1] += panel[1];
                    r
                })
                .collect(),
        );
        if self.votes.len() > self.cfg.red_confirm_window {
            self.votes.remove(0);
        }
        for b in blobs.iter_mut().filter(|b| b.accepted) {
            let mut r = b.bbox;
            r[0] += panel[0];
            r[1] += panel[1];
            b.confirm_votes = self
                .votes
                .iter()
                .filter(|frame| frame.iter().any(|old| iou(*old, r) >= 0.3))
                .count();
        }
        // Capture a second geometric observation before blocking on item OCR.
        // Text exclusions remain mandatory before confirming the red result.
        for b in blobs
            .iter_mut()
            .filter(|b| b.accepted && b.confirm_votes >= self.cfg.red_confirm_rounds)
        {
            let r = b.bbox;
            let pad = 20.max((r[2].min(r[3]) as f64 * 0.45) as i32);
            let x0 = (r[0] - pad).max(0);
            let y0 = (r[1] - pad).max(0);
            let text_rect = [x0, y0, r[0] + r[2] + pad - x0, r[1] + r[3] + pad - y0];
            let region = crop(&sub, text_rect);
            let mut hash = std::collections::hash_map::DefaultHasher::new();
            region.as_raw().hash(&mut hash);
            let key = hash.finish();
            let (text, name) = if let Some(pair) = self.cache.get(&key) {
                self.text_cache_hits += 1;
                pair.clone()
            } else {
                let start = Instant::now();
                let mut text = String::new();
                let mut name = String::new();
                for scale in [1., 1.5] {
                    for line in self.engine.recognize(&region, scale)? {
                        text.push_str(&line.text);
                        if name.is_empty() {
                            name = line.text;
                        }
                    }
                    self.item_ocr_calls += 1;
                }
                self.item_ms_total += start.elapsed().as_secs_f64() * 1000.;
                if self.cache.len() >= 64 {
                    self.cache.clear();
                }
                self.cache.insert(key, (text.clone(), name.clone()));
                (text, name)
            };
            b.ocr_text = text;
            b.ocr_name = name;
            if crate::keyword_filter::matches(&b.ocr_text, &self.cfg.red_exclude_words) {
                b.accepted = false;
                b.reject.push("排除词".into());
            }
        }
        for b in &mut blobs {
            b.bbox[0] += panel[0];
            b.bbox[1] += panel[1];
        }
        let mut confirmed = vec![];
        for b in blobs.iter_mut().filter(|b| b.accepted) {
            b.confirm_votes = self
                .votes
                .iter()
                .filter(|frame| frame.iter().any(|old| iou(*old, b.bbox) >= 0.3))
                .count();
            if b.confirm_votes >= self.cfg.red_confirm_rounds {
                confirmed.push(b.clone());
            }
        }
        self.candidates = blobs.into_iter().take(8).collect();
        if !confirmed.is_empty() {
            if let Some(s) = &mut self.session {
                s.red = true;
                s.red_blobs = confirmed.clone();
            }
        }
        Ok(confirmed)
    }
    pub fn end(&mut self, reason: &str, incomplete: bool) -> Step {
        let mut s = self.session.take().unwrap();
        s.end = Some(now());
        s.end_reason = Some(reason.into());
        let kind = if incomplete {
            "incomplete"
        } else if s.red {
            self.streak = 0;
            self.total += 1;
            "red"
        } else {
            self.streak += 1;
            self.total += 1;
            "clean"
        };
        self.template = None;
        self.votes.clear();
        self.misses = 0;
        self.gray = 0;
        self.last_ocr = None;
        Step {
            kind: Some(kind.into()),
            session: Some(s),
            visible: false,
            reds: vec![],
        }
    }
    pub fn step(&mut self, work: &RgbImage) -> Result<Step> {
        if self.session.is_none() {
            if let Some(line) = self.title(work)? {
                let n = line.bbox;
                self.idle_anchor = Some(n);
                self.votes.clear();
                self.gray = 0;
                self.misses = 0;
                let px = (n[2] as f64 * 0.55) as i32;
                let py = (n[3] as f64 * 0.5) as i32;
                let x0 = (n[0] - px).max(0);
                let y0 = (n[1] - py).max(0);
                let r = [x0, y0, n[0] + n[2] + px - x0, n[1] + n[3] + py - y0];
                self.template = Some((crop(work, r), r));
                self.session = Some(Session {
                    start: now(),
                    end: None,
                    end_reason: None,
                    ocr_text: line.text,
                    name_bbox: n,
                    panel_rect: panel_rect(n),
                    red: false,
                    red_blobs: vec![],
                    observations: 0,
                });
                let reds = self.scan(work, n)?;
                return Ok(Step {
                    kind: Some("start".into()),
                    session: self.session.clone(),
                    visible: true,
                    reds,
                });
            }
            return Ok(Step {
                kind: None,
                session: None,
                visible: false,
                reds: vec![],
            });
        }
        self.session.as_mut().unwrap().observations += 1;
        self.score = self
            .template
            .as_ref()
            .map_or(0., |(tpl, r)| ncc(work, tpl, *r));
        if self.score >= self.cfg.tpl_match_min {
            self.misses = 0;
            self.gray = 0;
        } else if self.score < self.cfg.tpl_gone_max {
            self.misses += 1;
            self.gray = 0;
        } else {
            self.gray += 1;
            if self.gray >= self.cfg.tpl_gray_max_rounds {
                self.misses += 1;
            }
        }
        if self.misses >= self.cfg.miss_need {
            return Ok(self.end("面板消失", false));
        }
        if self.score < self.cfg.tpl_match_min {
            self.votes.push(vec![]);
            if self.votes.len() > self.cfg.red_confirm_window {
                self.votes.remove(0);
            }
            return Ok(Step {
                kind: None,
                session: self.session.clone(),
                visible: false,
                reds: vec![],
            });
        }
        let n = self.session.as_ref().unwrap().name_bbox;
        let reds = self.scan(work, n)?;
        Ok(Step {
            kind: None,
            session: self.session.clone(),
            visible: true,
            reds,
        })
    }
}
pub fn panel_rect(n: Rect) -> Rect {
    [
        (n[0] - n[3] * 16).max(0),
        (n[1] - n[3] * 2).max(0),
        n[2] + n[3] * 32,
        n[3] * 18,
    ]
}
fn gray(p: &image::Rgb<u8>) -> f64 {
    p[0] as f64 * 0.299 + p[1] as f64 * 0.587 + p[2] as f64 * 0.114
}
fn ncc(work: &RgbImage, tpl: &RgbImage, r: Rect) -> f64 {
    if tpl.width() > work.width() || tpl.height() > work.height() {
        return 0.;
    }
    let mut samples = vec![];
    for y in (0..tpl.height()).step_by(2) {
        for x in (0..tpl.width()).step_by(2) {
            samples.push((x, y, gray(tpl.get_pixel(x, y))));
        }
    }
    let count = samples.len() as f64;
    let mean = samples.iter().map(|p| p.2).sum::<f64>() / count;
    let variance = samples.iter().map(|p| (p.2 - mean).powi(2)).sum::<f64>();
    if variance < 1e-6 {
        return 0.;
    }
    let mut best: f64 = 0.;
    let minx = (r[0] - 30).max(0) as u32;
    let miny = (r[1] - 30).max(0) as u32;
    let maxx = (r[0] + 30).min((work.width() - tpl.width()) as i32).max(0) as u32;
    let maxy = (r[1] + 30)
        .min((work.height() - tpl.height()) as i32)
        .max(0) as u32;
    for y in (miny..=maxy).step_by(2) {
        for x in (minx..=maxx).step_by(2) {
            let mut sum = 0.;
            let mut squares = 0.;
            let mut cov = 0.;
            for &(tx, ty, t) in &samples {
                let v = gray(work.get_pixel(x + tx, y + ty));
                sum += v;
                squares += v * v;
                cov += (t - mean) * v;
            }
            let sv = (squares - sum * sum / count).max(0.);
            if sv > 1e-6 {
                best = best.max(cov / (variance * sv).sqrt());
            }
        }
    }
    best
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    #[ignore = "requires private local feedback screenshots"]
    fn feedback_false_positives() {
        let base = std::path::PathBuf::from(
            std::env::var("AB_RED_PRIVATE_FIXTURES")
                .expect("set AB_RED_PRIVATE_FIXTURES to the private fixture directory"),
        );
        let mut failures = vec![];
        for (file, anchor, want) in [
            ("feedback_laptop.png", [35, 42, 107, 21], false),
            ("feedback_coin.png", [28, 30, 82, 15], false),
            ("feedback_gyro.png", [37, 56, 106, 28], true),
        ] {
            let image = image::open(base.join(file)).unwrap().to_rgb8();
            let mut tracker = Tracker::new(Config::default()).unwrap();
            tracker.scan(&image, anchor).unwrap();
            if !tracker.scan(&image, anchor).unwrap().is_empty() != want {
                failures.push(file);
            }
            // Exercise OCR -> panel validation -> tracking -> multi-frame confirmation.
            for scale in [0.75, 1., 1.5] {
                let image = imageops::resize(
                    &image,
                    (image.width() as f64 * scale) as u32,
                    (image.height() as f64 * scale) as u32,
                    imageops::FilterType::CatmullRom,
                );
                let mut tracker = Tracker::new(Config::default()).unwrap();
                assert_eq!(
                    tracker.step(&image).unwrap().kind.as_deref(),
                    Some("start"),
                    "{file} at {scale}"
                );
                for _ in 0..3 {
                    tracker.step(&image).unwrap();
                }
                assert_eq!(tracker.session.unwrap().red, want, "{file} at {scale}");
            }
        }
        let image = image::open(base.join("feedback_web_status.png"))
            .unwrap()
            .to_rgb8();
        let mut tracker = Tracker::new(Config::default()).unwrap();
        if tracker.step(&image).unwrap().kind.is_some() {
            failures.push("feedback_web_status.png");
        }
        let image = image::open(base.join("feedback_unsearched.png"))
            .unwrap()
            .to_rgb8();
        let mut tracker = Tracker::new(Config::default()).unwrap();
        if tracker.step(&image).unwrap().kind.is_some() {
            failures.push("feedback_unsearched.png");
        }
        assert!(
            failures.is_empty(),
            "incorrect feedback results: {failures:?}"
        );
    }
    #[test]
    fn title_requires_name_and_inventory_grid() {
        for text in ["保险箱", "小型保险箱", "电子保险箱", "机密保险箱"] {
            assert!(safe_title(text, "保险箱"));
        }
        for text in [
            "·等待保险箱出现",
            "等待保险箱",
            "打开保险箱",
            "保险箱次数",
            "正在检测保险箱",
        ] {
            assert!(!safe_title(text, "保险箱"));
        }
        let empty = RgbImage::from_pixel(600, 420, image::Rgb([20, 20, 20]));
        assert!(grid_cell(&empty, [35, 42, 100, 20]).is_none());
    }
    #[test]
    fn synthetic_rarity_and_actual_slot_size() {
        let mut grid = RgbImage::from_pixel(600, 420, image::Rgb([20, 20, 20]));
        for x in [33, 117, 201, 285, 369, 453] {
            for y in 94..410 {
                grid.put_pixel(x, y, image::Rgb([96, 96, 96]));
            }
        }
        let cell = grid_cell(&grid, [35, 42, 100, 20]).unwrap();
        assert_eq!(cell, 84.);
        for (color, side, want) in [
            ([90, 45, 45], 84, false),
            ([90, 45, 45], 168, true),
            ([90, 70, 64], 168, false),
        ] {
            let mut image = RgbImage::from_pixel(600, 420, image::Rgb([20, 20, 20]));
            for y in 100..100 + side {
                for x in 160..160 + side {
                    image.put_pixel(x, y, image::Rgb(color));
                }
            }
            let blobs = find_red_cells(&image, &Config::default(), [0; 4], cell);
            assert_eq!(blobs.iter().any(|blob| blob.accepted), want);
        }
    }
    #[test]
    fn real_panels() {
        let mut t = Tracker::new(Config::default()).unwrap();
        let base = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures");
        let solar = image::open(base.join("solar_false_positive.png"))
            .unwrap()
            .to_rgb8();
        for _ in 0..3 {
            assert!(t.scan(&solar, [37, 42, 105, 21]).unwrap().is_empty());
        }
        t.votes.clear();
        let vase = image::open(base.join("run_red_0006.png"))
            .unwrap()
            .to_rgb8();
        t.scan(&vase, [37, 42, 105, 21]).unwrap();
        assert!(!t.scan(&vase, [37, 42, 105, 21]).unwrap().is_empty());
        t.votes.clear();
        let moon = image::open(base.join("brief_red_work.png"))
            .unwrap()
            .to_rgb8();
        t.scan(&moon, [34, 155, 110, 29]).unwrap();
        assert!(!t.scan(&moon, [34, 155, 110, 29]).unwrap().is_empty());
    }
    #[test]
    fn temporal_regions() {
        assert!(iou([0, 0, 80, 100], [200, 0, 80, 100]) < 0.3);
        assert!(iou([0, 0, 80, 100], [2, 2, 80, 100]) > 0.3);
    }
    #[test]
    fn constant_template_has_no_identity() {
        let im = RgbImage::from_pixel(100, 100, image::Rgb([20, 20, 20]));
        assert_eq!(ncc(&im, &crop(&im, [0, 0, 30, 30]), [0, 0, 30, 30]), 0.);
    }
}
