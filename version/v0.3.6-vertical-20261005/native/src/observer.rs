//! Game-mode gate independent of the inventory work region.
use crate::core::{crop, Engine, Rect};
use anyhow::Result;
use image::RgbImage;
use serde_json::{json, Value};
use std::time::{Duration, Instant};

pub const FOOTER_ROI: [f64; 4] = [0.20, 0.88, 0.60, 0.12];

#[derive(Clone, Copy, PartialEq, Debug)]
enum Mode {
    Checking,
    Playing,
    Spectating,
    Settlement,
}

pub struct Observer {
    mode: Mode,
    words: Vec<String>,
    clear_rounds: usize,
    last_check: Option<Instant>,
    calls: usize,
    ms: f64,
}
impl Observer {
    pub fn new() -> Self {
        Self {
            mode: Mode::Checking,
            words: vec![],
            clear_rounds: 0,
            last_check: None,
            calls: 0,
            ms: 0.,
        }
    }
    pub fn due(&self) -> bool {
        self.last_check
            .map_or(true, |t| t.elapsed() >= Duration::from_millis(500))
    }
    pub fn paused(&self) -> bool {
        self.mode != Mode::Playing
    }
    pub fn state(&self) -> &'static str {
        match self.mode {
            Mode::Checking => "CHECKING",
            Mode::Playing => "PLAYING",
            Mode::Spectating => "SPECTATING",
            Mode::Settlement => "SETTLEMENT",
        }
    }
    pub fn reason(&self) -> &'static str {
        match self.mode {
            Mode::Checking => "检查游戏底部状态，暂停记录",
            Mode::Playing => "正常检测",
            Mode::Spectating => "观战中，暂停记录",
            Mode::Settlement => "结算界面，暂停记录",
        }
    }
    fn observe(&mut self, text: &str) {
        let text: String = text.chars().filter(|c| !c.is_whitespace()).collect();
        let words: Vec<String> = ["观战", "结算"]
            .into_iter()
            .filter(|word| text.contains(word))
            .map(str::to_owned)
            .collect();
        if !words.is_empty() {
            self.mode = if words.iter().any(|w| w == "观战") {
                Mode::Spectating
            } else {
                Mode::Settlement
            };
            self.words = words;
            self.clear_rounds = 0;
        } else if self.paused() {
            self.clear_rounds += 1;
            if self.clear_rounds >= 2 {
                self.mode = Mode::Playing;
                self.words.clear();
            }
        }
    }
    pub fn unavailable(&mut self) {
        self.clear_rounds = 0;
        // Occlusion is not evidence that spectator mode has ended.
        if self.mode == Mode::Playing {
            self.mode = Mode::Checking;
        }
    }
    pub fn check(&mut self, engine: &Engine, footer: &RgbImage) -> Result<()> {
        let start = Instant::now();
        let mut text = String::new();
        for scale in [1., 1.5] {
            let lines = engine.recognize(footer, scale)?;
            self.calls += 1;
            for line in lines {
                text.push_str(&line.text);
                text.push('\n');
            }
            if text.contains("观战") || text.contains("结算") {
                break;
            }
        }
        self.ms += start.elapsed().as_secs_f64() * 1000.;
        self.last_check = Some(Instant::now());
        self.observe(&text);
        Ok(())
    }
    pub fn footer(image: &RgbImage) -> RgbImage {
        let [x, y, w, h] = FOOTER_ROI;
        let r: Rect = [
            (x * image.width() as f64).round() as i32,
            (y * image.height() as f64).round() as i32,
            (w * image.width() as f64).round() as i32,
            (h * image.height() as f64).round() as i32,
        ];
        crop(image, r)
    }
    pub fn status(&self) -> Value {
        json!({"play_state":self.state(),"play_state_text":self.reason(),
            "recording_paused":self.paused(),"mode_keywords":self.words,
            "mode_ocr_calls":self.calls,"mode_ocr_ms":if self.calls>0 {self.ms/self.calls as f64} else {0.}})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn either_keyword_pauses_and_two_clear_checks_resume() {
        let mut o = Observer::new();
        assert!(o.paused());
        o.observe("");
        o.observe("");
        assert!(!o.paused());
        o.observe("当前观 战\n结算");
        assert_eq!(o.state(), "SPECTATING");
        o.observe("");
        assert!(o.paused());
        o.unavailable();
        o.observe("");
        assert!(o.paused());
        o.observe("");
        assert!(!o.paused());
        o.observe("结算");
        assert_eq!(o.state(), "SETTLEMENT");
        o.observe("");
        o.observe("观战中");
        assert!(o.paused());
        o.observe("");
        o.observe("");
        assert!(!o.paused());
    }
    #[test]
    #[ignore = "requires private spectator screenshot and Windows Chinese OCR"]
    fn real_spectator_footer() {
        let engine = Engine::new().unwrap();
        let image = image::open(
            std::path::PathBuf::from(std::env::var("AB_RED_PRIVATE_FIXTURES").unwrap())
                .join("feedback_spectator.png"),
        )
        .unwrap()
        .to_rgb8();
        let mut o = Observer::new();
        o.check(&engine, &Observer::footer(&image)).unwrap();
        assert_eq!(o.state(), "SPECTATING");
        assert!(o.words.contains(&"观战".to_owned()) && o.words.contains(&"结算".to_owned()));
    }
}
