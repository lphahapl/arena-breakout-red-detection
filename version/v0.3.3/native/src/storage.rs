use anyhow::{bail, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    sync::Mutex,
};

pub fn valid_run(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 80
        && name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}
pub struct Store {
    pub root: PathBuf,
    db: Mutex<Connection>,
}
impl Store {
    pub fn new(root: &Path) -> Result<Self> {
        fs::create_dir_all(root)?;
        let db = Connection::open(root.join("history.sqlite3"))?;
        db.busy_timeout(std::time::Duration::from_secs(10))?;
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,metadata TEXT NOT NULL DEFAULT '{}',import_size INTEGER NOT NULL DEFAULT -1,import_mtime INTEGER NOT NULL DEFAULT -1);
            CREATE TABLE IF NOT EXISTS events(run TEXT NOT NULL,seq INTEGER NOT NULL,record TEXT NOT NULL,review TEXT,PRIMARY KEY(run,seq),FOREIGN KEY(run) REFERENCES runs(id));")?;
        let store = Self {
            root: root.into(),
            db: Mutex::new(db),
        };
        store.import()?;
        for name in store.names()? {
            let mut r = store.metadata(&name)?;
            if r["status"] == "running" {
                r["status"] = json!("interrupted");
                store.update(&name, r)?;
            }
        }
        Ok(store)
    }
    pub fn import(&self) -> Result<()> {
        for directory in fs::read_dir(&self.root)? {
            let directory = directory?;
            if !directory.file_type()?.is_dir() {
                continue;
            }
            let name = directory.file_name().to_string_lossy().to_string();
            if !valid_run(&name) {
                continue;
            }
            let path = directory.path().join("events.jsonl");
            let db = self.db.lock().unwrap();
            db.execute(
                "INSERT OR IGNORE INTO runs(id,metadata)VALUES(?,?)",
                params![name, json!({"status":"legacy"}).to_string()],
            )?;
            if let Ok(file) = fs::File::open(&path) {
                for line in BufReader::new(file).lines() {
                    let Ok(line) = line else {
                        continue;
                    };
                    let Ok(v) = serde_json::from_str::<Value>(&line) else {
                        continue;
                    };
                    let Some(seq) = v["seq"].as_i64() else {
                        continue;
                    };
                    db.execute(
                        "INSERT OR IGNORE INTO events(run,seq,record)VALUES(?,?,?)",
                        params![name, seq, v.to_string()],
                    )?;
                }
            }
        }
        Ok(())
    }
    fn metadata(&self, name: &str) -> Result<Value> {
        let db = self.db.lock().unwrap();
        let text: Option<String> = db
            .query_row("SELECT metadata FROM runs WHERE id=?", [name], |row| {
                row.get(0)
            })
            .optional()?;
        Ok(text
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or(json!({})))
    }
    pub fn update(&self, name: &str, fields: Value) -> Result<()> {
        if !valid_run(name) {
            bail!("运行编号错误");
        }
        let db = self.db.lock().unwrap();
        let text: Option<String> = db
            .query_row("SELECT metadata FROM runs WHERE id=?", [name], |row| {
                row.get(0)
            })
            .optional()?;
        let mut meta: Value = text
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or(json!({}));
        if let Some(fields) = fields.as_object() {
            for (k, v) in fields {
                meta[k] = v.clone();
            }
        }
        db.execute("INSERT INTO runs(id,metadata)VALUES(?,?) ON CONFLICT(id)DO UPDATE SET metadata=excluded.metadata",params![name,meta.to_string()])?;
        Ok(())
    }
    pub fn append(&self, name: &str, event: &Value) -> Result<()> {
        if !valid_run(name) {
            bail!("运行编号错误");
        }
        let db = self.db.lock().unwrap();
        db.execute("INSERT OR IGNORE INTO runs(id)VALUES(?)", [name])?;
        db.execute("INSERT INTO events(run,seq,record)VALUES(?,?,?) ON CONFLICT(run,seq)DO UPDATE SET record=excluded.record",params![name,event["seq"].as_i64(),event.to_string()])?;
        Ok(())
    }
    pub fn names(&self) -> Result<Vec<String>> {
        let db = self.db.lock().unwrap();
        let mut query = db.prepare("SELECT id FROM runs ORDER BY id DESC")?;
        let result = query
            .query_map([], |row| row.get(0))?
            .collect::<std::result::Result<Vec<String>, _>>()?;
        Ok(result)
    }
    pub fn events(&self, name: &str, since: i64) -> Result<Value> {
        if !valid_run(name) {
            bail!("运行编号错误");
        }
        let db = self.db.lock().unwrap();
        let mut query =
            db.prepare("SELECT record,review FROM events WHERE run=? AND seq>? ORDER BY seq")?;
        let mut events = vec![];
        let mut rows = query.query(params![name, since])?;
        while let Some(row) = rows.next()? {
            let text: String = row.get(0)?;
            let label: Option<String> = row.get(1)?;
            let mut v: Value = serde_json::from_str(&text)?;
            v["review"] = json!(label);
            events.push(v);
        }
        let last: i64 = db.query_row(
            "SELECT COALESCE(MAX(seq),0)FROM events WHERE run=?",
            [name],
            |row| row.get(0),
        )?;
        Ok(json!({"events":events,"last_seq":last}))
    }
    pub fn review(&self, name: &str, seq: i64, label: Option<&str>) -> Result<()> {
        if !valid_run(name)
            || !matches!(
                label,
                None | Some("red") | Some("clean") | Some("uncertain")
            )
        {
            bail!("复核参数错误");
        }
        let db = self.db.lock().unwrap();
        let record: Option<String> = db
            .query_row(
                "SELECT record FROM events WHERE run=? AND seq=?",
                params![name, seq],
                |row| row.get(0),
            )
            .optional()?;
        let record: Value =
            serde_json::from_str(&record.ok_or_else(|| anyhow::anyhow!("记录不存在"))?)?;
        if !matches!(
            record["kind"].as_str(),
            Some("red") | Some("clean") | Some("incomplete")
        ) {
            bail!("开箱记录不能复核");
        }
        db.execute(
            "UPDATE events SET review=? WHERE run=? AND seq=?",
            params![label, name, seq],
        )?;
        Ok(())
    }
    pub fn stats(&self) -> Result<Value> {
        let db = self.db.lock().unwrap();
        let mut query = db.prepare("SELECT run,seq,COALESCE(json_extract(record,'$.kind'),'') FROM events ORDER BY run,seq")?;
        let entries = query
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<std::result::Result<Vec<(String, i64, String)>, _>>()?;
        Ok(crate::stats::best_ten(entries))
    }
    /// Hold the database lock for a consistent metadata/events/reviews feedback snapshot.
    pub fn snapshot(&self, name: &str) -> Result<Value> {
        if !valid_run(name) {
            bail!("运行编号错误");
        }
        let db = self.db.lock().unwrap();
        let meta: String = db
            .query_row("SELECT metadata FROM runs WHERE id=?", [name], |r| r.get(0))
            .optional()?
            .ok_or_else(|| anyhow::anyhow!("记录不存在"))?;
        let mut query = db.prepare("SELECT record,review FROM events WHERE run=? ORDER BY seq")?;
        let mut events = vec![];
        let mut rows = query.query([name])?;
        while let Some(row) = rows.next()? {
            let text: String = row.get(0)?;
            let review: Option<String> = row.get(1)?;
            let mut event: Value = serde_json::from_str(&text)?;
            event["review"] = json!(review);
            events.push(event);
        }
        Ok(json!({"id":name,"metadata":serde_json::from_str::<Value>(&meta)?,"events":events}))
    }
    pub fn run(&self, name: &str) -> Result<Option<Value>> {
        if !valid_run(name) {
            bail!("运行编号错误");
        }
        let db = self.db.lock().unwrap();
        let meta: Option<String> = db
            .query_row("SELECT metadata FROM runs WHERE id=?", [name], |row| {
                row.get(0)
            })
            .optional()?;
        let Some(meta) = meta else {
            return Ok(None);
        };
        let mut v: Value = serde_json::from_str(&meta)?;
        let mut query = db.prepare(
            "SELECT
            COUNT(*),COALESCE(SUM(json_extract(record,'$.kind')='red'),0),
            COALESCE(SUM(json_extract(record,'$.kind')='clean'),0),
            COALESCE(SUM(json_extract(record,'$.kind')='incomplete'),0),
            COALESCE(SUM(review IS NOT NULL),0),
            COALESCE(SUM(json_extract(record,'$.kind')='red'AND review='clean'),0),
            COALESCE(SUM(json_extract(record,'$.kind')='clean'AND review='red'),0),
            MAX(json_extract(record,'$.t')) FROM events WHERE run=?",
        )?;
        let counts: (i64, i64, i64, i64, i64, i64, i64, Option<String>) =
            query.query_row([name], |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                    r.get(7)?,
                ))
            })?;
        v["id"] = json!(name);
        v["event_count"] = json!(counts.0);
        v["counts"] = json!({"red":counts.1,"clean":counts.2,"incomplete":counts.3});
        v["total"] = json!(counts.1 + counts.2);
        v["reviewed"] = json!(counts.4);
        v["false_positive"] = json!(counts.5);
        v["missed"] = json!(counts.6);
        v["last_event"] = json!(counts.7);
        v["red_rate"] = if counts.1 + counts.2 > 0 {
            json!(counts.1 as f64 / (counts.1 + counts.2) as f64 * 100.)
        } else {
            Value::Null
        };
        Ok(Some(v))
    }
    pub fn list(&self, limit: usize, offset: usize) -> Result<Value> {
        let names = self.names()?;
        let total = names.len();
        let mut runs = vec![];
        for name in names.into_iter().skip(offset).take(limit) {
            if let Some(r) = self.run(&name)? {
                runs.push(r);
            }
        }
        Ok(json!({"runs":runs,"total":total,"limit":limit,"offset":offset}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn persistence_and_review() {
        let root = std::env::temp_dir().join(format!("red-native-store-{}", std::process::id()));
        fs::create_dir_all(root.join("legacy")).unwrap();
        fs::write(
            root.join("legacy/events.jsonl"),
            "{\"seq\":1,\"kind\":\"red\"}\n{\"seq\":",
        )
        .unwrap();
        {
            let s = Store::new(&root).unwrap();
            s.review("legacy", 1, Some("clean")).unwrap();
            s.update("empty", json!({"status":"running"})).unwrap();
            s.append("new", &json!({"seq":1,"kind":"incomplete"}))
                .unwrap();
            assert_eq!(s.run("new").unwrap().unwrap()["total"], 0);
        }
        {
            let s = Store::new(&root).unwrap();
            assert_eq!(s.run("legacy").unwrap().unwrap()["false_positive"], 1);
            assert_eq!(s.run("empty").unwrap().unwrap()["status"], "interrupted");
            assert!(s.review("legacy", 1, Some("bad")).is_err());
            assert!(s.events("../outside", 0).is_err());
        }
        fs::remove_dir_all(root).unwrap();
    }
}
