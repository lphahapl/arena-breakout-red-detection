use serde_json::{json, Value};
use std::collections::VecDeque;

/// Windows never span runs or incomplete observations. Reviews do not change model rates.
pub fn best_ten(entries: impl IntoIterator<Item = (String, i64, String)>) -> Value {
    let mut window = VecDeque::new();
    let mut run = String::new();
    let mut reds = 0usize;
    let mut best_reds = 0usize;
    let mut settled = 0usize;
    let mut longest = 0usize;
    let mut streak = 0usize;
    let mut best = Value::Null;
    for (id, seq, kind) in entries {
        if run != id {
            run = id;
            window.clear();
            reds = 0;
            streak = 0;
        }
        if kind == "incomplete" {
            window.clear();
            reds = 0;
            streak = 0;
            continue;
        }
        if kind != "red" && kind != "clean" {
            continue;
        }
        settled += 1;
        streak += 1;
        longest = longest.max(streak);
        let red = kind == "red";
        reds += usize::from(red);
        window.push_back((seq, red));
        if window.len() > 10 {
            reds -= usize::from(window.pop_front().unwrap().1);
        }
        // Equal scores prefer the latest window, including a valid 0/10 record.
        if window.len() == 10 && (best.is_null() || reds >= best_reds) {
            best_reds = reds;
            best = json!({"run":run,"start_seq":window[0].0,"end_seq":seq,"red_count":reds,"red_rate":reds*10,
                "boxes":window.iter().map(|(seq,red)|json!({"seq":seq,"red":red})).collect::<Vec<_>>()});
        }
    }
    json!({"best_ten":best,"settled":settled,"longest_segment":longest,"window_size":10})
}

#[cfg(test)]
mod tests {
    use super::*;
    fn rows(run: &str, kinds: &[&str]) -> Vec<(String, i64, String)> {
        kinds
            .iter()
            .enumerate()
            .map(|(i, k)| (run.into(), i as i64 + 1, (*k).into()))
            .collect()
    }
    #[test]
    fn sliding_window_and_latest_tie() {
        let data = best_ten(rows(
            "a",
            &[
                "clean", "red", "red", "red", "red", "red", "red", "red", "red", "red", "red",
                "clean",
            ],
        ));
        assert_eq!(data["best_ten"]["red_rate"], 100);
        assert_eq!(data["best_ten"]["start_seq"], 2);
        let tied = best_ten(rows("a", &["clean"; 11]));
        assert_eq!(tied["best_ten"]["red_rate"], 0);
        assert_eq!(tied["best_ten"]["start_seq"], 2);
    }
    #[test]
    fn run_boundaries_and_incomplete_break_the_window() {
        let mut entries = rows("a", &["red"; 6]);
        entries.extend(rows("b", &["red"; 6]));
        assert!(best_ten(entries)["best_ten"].is_null());
        assert!(best_ten(rows(
            "a",
            &[
                "red",
                "red",
                "red",
                "red",
                "red",
                "incomplete",
                "red",
                "red",
                "red",
                "red",
                "red"
            ]
        ))["best_ten"]
            .is_null());
    }
    #[test]
    fn starts_are_ignored_and_ten_required() {
        assert!(best_ten(rows("a", &["red"; 9]))["best_ten"].is_null());
        let mut entries = rows("a", &["red"; 10]);
        entries.insert(4, ("a".into(), 0, "start".into()));
        let value = best_ten(entries);
        assert_eq!(value["best_ten"]["red_count"], 10);
        assert_eq!(value["settled"], 10);
    }
}
