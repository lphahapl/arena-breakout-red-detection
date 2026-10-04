use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::{
    os::windows::process::CommandExt,
    process::Command,
    sync::{Arc, Mutex},
    thread,
};

fn encoded_command(source: &str) -> String {
    let bytes: Vec<u8> = source.encode_utf16().flat_map(u16::to_le_bytes).collect();
    let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::new();
    for part in bytes.chunks(3) {
        let n = ((part[0] as u32) << 16)
            | ((part.get(1).copied().unwrap_or(0) as u32) << 8)
            | part.get(2).copied().unwrap_or(0) as u32;
        output.push(alphabet[((n >> 18) & 63) as usize] as char);
        output.push(alphabet[((n >> 12) & 63) as usize] as char);
        output.push(if part.len() > 1 {
            alphabet[((n >> 6) & 63) as usize] as char
        } else {
            '='
        });
        output.push(if part.len() > 2 {
            alphabet[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    output
}
fn invoke(install: bool) -> Result<Value> {
    let source = format!(
        "$Install=${};\n{}",
        if install { "true" } else { "false" },
        include_str!("../../environment.ps1")
    );
    let shell = std::path::PathBuf::from(std::env::var("SystemRoot")?)
        .join("System32/WindowsPowerShell/v1.0/powershell.exe");
    let output = Command::new(shell)
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            &encoded_command(&source),
        ])
        .creation_flags(0x08000000)
        .output()?;
    if !output.status.success() {
        bail!(
            "Windows 环境检测失败：{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(serde_json::from_str(
        String::from_utf8_lossy(&output.stdout)
            .trim_start_matches('\u{feff}')
            .trim(),
    )?)
}

fn prepare(
    mut execute: impl FnMut(bool) -> Result<Value>,
    mut progress: impl FnMut(Value),
    verify: impl FnOnce() -> Result<()>,
) -> Result<Value> {
    let mut result = execute(false)?;
    if result["status"] == "missing" {
        progress(
            json!({"status":"installing", "message":"正在安装简体中文 OCR。请允许 Windows 管理员授权，并保持联网；可能需要几分钟。"}),
        );
        result = execute(true)?;
    }
    if result["status"] == "ready" {
        verify()?;
    } else if result["status"] != "error" {
        bail!("安装未完成，中文 OCR 仍不可用");
    }
    Ok(result)
}

pub struct Environment {
    state: Mutex<Value>,
}
impl Environment {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(json!({"status":"pending", "message":"等待环境检测"})),
        })
    }
    pub fn status(&self) -> Value {
        self.state.lock().unwrap().clone()
    }
    pub fn start(self: &Arc<Self>) -> bool {
        let mut state = self.state.lock().unwrap();
        if state["status"] == "checking" || state["status"] == "installing" {
            return false;
        }
        *state = json!({"status":"checking", "message":"正在检测中文 OCR 环境…"});
        drop(state);
        let env = self.clone();
        thread::spawn(move || {
            let result = prepare(
                invoke,
                |s| *env.state.lock().unwrap() = s,
                || {
                    crate::core::Engine::new()?;
                    Ok(())
                },
            );
            *env.state.lock().unwrap() = match result {
                Ok(s) => s,
                Err(e) => json!({"status":"error", "message":format!("环境检测或安装失败：{e}")}),
            };
        });
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn existing_environment_never_installs() {
        let mut calls = vec![];
        let result = prepare(
            |i| {
                calls.push(i);
                Ok(json!({"status":"ready"}))
            },
            |_| panic!("no install progress"),
            || Ok(()),
        )
        .unwrap();
        assert_eq!(result["status"], "ready");
        assert_eq!(calls, vec![false]);
        assert_eq!(encoded_command("A"), "QQA=");
        assert_eq!(encoded_command("AB"), "QQBCAA==");
        assert_eq!(encoded_command("中"), "LU4=");
    }
    #[test]
    fn missing_environment_installs_and_verifies() {
        let mut calls = vec![];
        let mut progress = vec![];
        let mut verified = false;
        let r = prepare(
            |i| {
                calls.push(i);
                Ok(json!({"status":if i {"ready"} else {"missing"}}))
            },
            |s| progress.push(s),
            || {
                verified = true;
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(r["status"], "ready");
        assert_eq!(calls, vec![false, true]);
        assert_eq!(progress[0]["status"], "installing");
        assert!(verified);
    }
    #[test]
    fn rejected_or_failed_install_does_not_become_ready() {
        let r = prepare(
            |i| Ok(json!({"status":if i {"error"} else {"missing"}, "message":"管理员授权已取消"})),
            |_| {},
            || panic!("cannot verify failed install"),
        )
        .unwrap();
        assert_eq!(r["status"], "error");
        assert!(prepare(
            |_| Ok(json!({"status":"ready"})),
            |_| {},
            || bail!("engine failed")
        )
        .is_err());
        assert!(prepare(|_| bail!("PowerShell unavailable"), |_| {}, || Ok(())).is_err());
    }
}
