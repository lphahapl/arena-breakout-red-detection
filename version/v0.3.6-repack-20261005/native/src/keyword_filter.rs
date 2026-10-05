use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::path::Path;

const DEFAULT_JSON: &str = include_str!("../../red_filter.json");

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Rules {
    exclude_words: Vec<String>,
}

pub fn parse(text: &str) -> Result<Vec<String>> {
    let rules: Rules = serde_json::from_str(text.trim_start_matches('\u{feff}'))?;
    let mut words = Vec::new();
    for word in rules.exclude_words {
        if word.is_empty() || word.chars().any(char::is_whitespace) {
            bail!("exclude_words 的词语不能为空或含空白；[] 可关闭词语屏蔽");
        }
        if !words.contains(&word) {
            words.push(word);
        }
    }
    Ok(words)
}

pub fn defaults() -> Vec<String> {
    parse(DEFAULT_JSON).expect("bundled red_filter.json must be valid")
}

pub fn load(base: &Path) -> Result<Vec<String>> {
    let path = base.join("red_filter.json");
    // Standalone exe also creates an editable default file. Never replace a user file.
    if !path.exists() {
        use std::io::Write;
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(mut file) => file.write_all(DEFAULT_JSON.as_bytes())?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => {
                return Err(error).with_context(|| format!("无法创建屏蔽词配置 {}", path.display()))
            }
        }
    }
    let text = std::fs::read_to_string(&path)
        .with_context(|| format!("无法读取屏蔽词配置 {}", path.display()))?;
    parse(&text).with_context(|| format!("屏蔽词配置 {} 无效", path.display()))
}

pub fn matches(text: &str, words: &[String]) -> bool {
    words.iter().any(|word| text.contains(word))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn navigation_is_allowed_and_lab_is_blocked() {
        let words = defaults();
        assert!(!matches("航天导航仪", &words));
        assert!(matches("航天实验室", &words));
        assert!(matches("行星之子", &words));
        assert!(!matches("航天实验", &words));
    }
    #[test]
    fn record_player_is_excluded_without_blocking_other_names() {
        let words = defaults();
        assert!(matches("唱片机", &words));
        // Both OCR scales are concatenated before checking the configured words.
        assert!(matches("埕片机唱片机", &words));
        for name in ["航天导航仪", "古董花瓶", "古董茶壶", "珍藏唱片"] {
            assert!(!matches(name, &words), "{name}");
        }
        assert!(!matches(
            "唱片机",
            &parse("{\"exclude_words\":[]}").unwrap()
        ));
    }
    #[test]
    fn editable_rules_and_empty_list() {
        let words = parse("\u{feff}{\"exclude_words\":[\"定制词\",\"定制词\"]}").unwrap();
        assert_eq!(words.len(), 1);
        assert!(matches("自定义定制词", &words));
        assert!(!matches("实验室", &words));
        assert!(!matches(
            "行星之子",
            &parse("{\"exclude_words\":[]}").unwrap()
        ));
    }
    #[test]
    fn invalid_rules_are_rejected() {
        for text in [
            "{",
            "{}",
            "{\"exclude_words\":\"实验室\"}",
            "{\"exclude_words\":[\"\"]}",
            "{\"exclude_words\":[\" \"]}",
            "{\"exclude_words\":[1]}",
            "{\"exclude_words\":[],\"typo\":true}",
        ] {
            assert!(parse(text).is_err(), "{text}");
        }
    }
    #[test]
    fn file_defaults_and_updates() {
        let base = std::env::temp_dir().join(format!("red-filter-test-{}", std::process::id()));
        std::fs::create_dir_all(&base).unwrap();
        assert_eq!(load(&base).unwrap(), defaults());
        std::fs::write(base.join("red_filter.json"), "{\"exclude_words\":[]}").unwrap();
        assert!(load(&base).unwrap().is_empty());
        std::fs::write(base.join("red_filter.json"), "{").unwrap();
        assert!(load(&base).is_err());
        std::fs::remove_file(base.join("red_filter.json")).unwrap();
        std::fs::remove_dir(base).unwrap();
    }
}
