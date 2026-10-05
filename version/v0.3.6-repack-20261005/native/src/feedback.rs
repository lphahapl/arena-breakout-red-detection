use anyhow::{bail, Context, Result};
use flate2::{write::DeflateEncoder, Compression};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs,
    io::{Cursor, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};

struct Entry {
    name: Vec<u8>,
    method: u16,
    crc: u32,
    size: u32,
    packed: u32,
    offset: u32,
}
struct Zip {
    file: fs::File,
    entries: Vec<Entry>,
}
fn u16le(file: &mut fs::File, n: u16) -> Result<()> {
    file.write_all(&n.to_le_bytes())?;
    Ok(())
}
fn u32le(file: &mut fs::File, n: u32) -> Result<()> {
    file.write_all(&n.to_le_bytes())?;
    Ok(())
}
fn small(n: u64) -> Result<u32> {
    u32::try_from(n).context("反馈包超过 ZIP32 大小限制，请选择较短的一次运行")
}
impl Zip {
    fn add(&mut self, name: &str, mut reader: impl Read, compress: bool) -> Result<()> {
        if name.contains("..")
            || name.starts_with('/')
            || name.contains('\\')
            || name.len() > u16::MAX as usize
        {
            bail!("反馈文件名无效");
        }
        let offset = small(self.file.stream_position()?)?;
        let method = if compress { 8 } else { 0 };
        u32le(&mut self.file, 0x04034b50)?;
        for n in [20, 0x800, method, 0, 33] {
            u16le(&mut self.file, n)?;
        }
        for _ in 0..3 {
            u32le(&mut self.file, 0)?;
        }
        u16le(&mut self.file, name.len() as u16)?;
        u16le(&mut self.file, 0)?;
        self.file.write_all(name.as_bytes())?;
        let start = self.file.stream_position()?;
        let mut hash = crc32fast::Hasher::new();
        let mut size = 0u64;
        let mut buffer = [0u8; 65536];
        if compress {
            let mut output = DeflateEncoder::new(&mut self.file, Compression::fast());
            loop {
                let n = reader.read(&mut buffer)?;
                if n == 0 {
                    break;
                }
                size += n as u64;
                small(size)?;
                hash.update(&buffer[..n]);
                output.write_all(&buffer[..n])?;
            }
            output.finish()?;
        } else {
            loop {
                let n = reader.read(&mut buffer)?;
                if n == 0 {
                    break;
                }
                size += n as u64;
                small(size)?;
                hash.update(&buffer[..n]);
                self.file.write_all(&buffer[..n])?;
            }
        }
        let end = self.file.stream_position()?;
        let packed = small(end - start)?;
        let size = small(size)?;
        let crc = hash.finalize();
        self.file.seek(SeekFrom::Start(offset as u64 + 14))?;
        for n in [crc, packed, size] {
            u32le(&mut self.file, n)?;
        }
        self.file.seek(SeekFrom::Start(end))?;
        self.entries.push(Entry {
            name: name.as_bytes().to_vec(),
            method,
            crc,
            size,
            packed,
            offset,
        });
        Ok(())
    }
    fn json(&mut self, name: &str, value: &Value) -> Result<()> {
        self.add(name, Cursor::new(serde_json::to_vec_pretty(value)?), true)
    }
    fn finish(mut self) -> Result<()> {
        let start = small(self.file.stream_position()?)?;
        for e in &self.entries {
            u32le(&mut self.file, 0x02014b50)?;
            for n in [20, 20, 0x800, e.method, 0, 33] {
                u16le(&mut self.file, n)?;
            }
            for n in [e.crc, e.packed, e.size] {
                u32le(&mut self.file, n)?;
            }
            for n in [e.name.len() as u16, 0, 0, 0, 0] {
                u16le(&mut self.file, n)?;
            }
            u32le(&mut self.file, 0)?;
            u32le(&mut self.file, e.offset)?;
            self.file.write_all(&e.name)?;
        }
        let len = small(self.file.stream_position()? - start as u64)?;
        let count = u16::try_from(self.entries.len()).context("反馈文件数量超过 ZIP32 限制")?;
        u32le(&mut self.file, 0x06054b50)?;
        for n in [0, 0, count, count] {
            u16le(&mut self.file, n)?;
        }
        u32le(&mut self.file, len)?;
        u32le(&mut self.file, start)?;
        u16le(&mut self.file, 0)?;
        self.file.sync_all()?;
        Ok(())
    }
}

/// Export one run and its referenced evidence, never the entire personal database.
pub fn export(
    base: &Path,
    store: &crate::storage::Store,
    run: Option<&str>,
    state: Value,
    environment: Value,
    config: Value,
) -> Result<PathBuf> {
    let snapshot = run.map(|id| store.snapshot(id)).transpose()?;
    let output = base.join("exports");
    fs::create_dir_all(&output)?;
    let name = format!(
        "feedback-v{}-{}-{}.zip",
        env!("CARGO_PKG_VERSION"),
        run.unwrap_or("environment"),
        chrono::Local::now().format("%Y%m%d-%H%M%S-%6f")
    );
    let target = output.join(name);
    let partial = target.with_extension("zip.part");
    let result = (|| {
        let mut zip = Zip {
            file: fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&partial)?,
            entries: vec![],
        };
        zip.json("diagnostics.json",&json!({"version":env!("CARGO_PKG_VERSION"),"exported_at":crate::core::now(),"platform":std::env::consts::OS,"architecture":std::env::consts::ARCH,"selected_run":run,"state":state,"environment":environment,"config":config}))?;
        zip.json("personal_stats.json", &store.stats()?)?;
        let mut missing = vec![];
        if let Some(snapshot) = snapshot {
            zip.json("run.json", &snapshot)?;
            let id = run.unwrap();
            let root = store.root.canonicalize()?;
            let directory = root.join(id);
            let mut files: BTreeSet<String> = ["events.jsonl".into(), "trace.jsonl".into()].into();
            for event in snapshot["events"].as_array().unwrap() {
                if let Some(shots) = event["shots"].as_array() {
                    for shot in shots.iter().filter_map(Value::as_str) {
                        if !shot.ends_with(".png")
                            || shot.contains('/')
                            || shot.contains('\\')
                            || shot.contains("..")
                        {
                            bail!("检测截图文件名无效");
                        }
                        files.insert(shot.into());
                    }
                }
            }
            let canonical_directory = directory.canonicalize().ok();
            if canonical_directory
                .as_ref()
                .is_some_and(|p| !p.starts_with(&root))
            {
                bail!("反馈目录越界");
            }
            for name in files {
                let source = directory.join(&name);
                if !source.exists() {
                    missing.push(name);
                    continue;
                }
                let canonical = source.canonicalize()?;
                if !canonical_directory
                    .as_ref()
                    .is_some_and(|p| canonical.starts_with(p))
                {
                    bail!("反馈文件越界");
                }
                let file = fs::File::open(source)?;
                let len = file.metadata()?.len();
                zip.add(
                    &format!("run/{name}"),
                    file.take(len),
                    !name.ends_with(".png"),
                )?;
            }
        }
        zip.json("missing_files.json", &json!(missing))?;
        zip.add("反馈说明.txt",Cursor::new("这是暗区红品检测的反馈包。请附上问题发生时间、预期结果和实际结果。\n包含所选运行的事件、人工复核、截图、采样日志、环境状态和检测参数。run.json 是一致的事件快照；运行中导出的原始日志可能包含更新的数据或不完整末行。\n未找到的原始文件列在 missing_files.json。包内统计按自动检测结果计算。\n".as_bytes()),true)?;
        zip.finish()?;
        fs::rename(&partial, &target)?;
        Ok(target.clone())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&partial);
    }
    result
}
