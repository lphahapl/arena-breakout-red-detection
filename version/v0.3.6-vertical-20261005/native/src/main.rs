#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod capture;
mod core;
mod environment;
mod feedback;
mod keyword_filter;
mod observer;
mod runtime;
mod stats;
mod storage;
use anyhow::{bail, Result};
use core::{Config, Step, Tracker};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Cursor, Read, Write},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tiny_http::{Header, Request, Response, Server, StatusCode};

struct App {
    environment: Arc<environment::Environment>,
    store: storage::Store,
    state: Mutex<Value>,
    config: Mutex<Config>,
    window: Mutex<String>,
    stop: AtomicBool,
    base: PathBuf,
    export_lock: Mutex<()>,
}
fn initial() -> Value {
    json!({"running":false,"state":"IDLE","play_state":"CHECKING","play_state_text":"等待开始检测","recording_paused":true,"mode_keywords":[],"mode_ocr_calls":0,"mode_ocr_ms":0.,"streak":0,"total":0,"current":null,"target":null,"error":null,"misses":0,"miss_need":3,"obs":0,"ocr_ms":0.,"ocr_calls":0,"foreground":true,"run_dir":null})
}
fn check_mode(
    window: &capture::Window,
    observer: &mut observer::Observer,
    engine: &core::Engine,
) -> Result<()> {
    let rect = capture::region(window, Some(observer::FOOTER_ROI))?;
    if capture::visible(window, rect) {
        observer.check(engine, &capture::grab(rect)?)?;
    } else {
        observer.unavailable();
    }
    Ok(())
}
fn metrics(t: &Tracker, obs: usize, busy: f64) -> Value {
    json!({"obs":obs,"ocr_calls":t.ocr_calls,"ocr_ms":if t.ocr_calls>0{t.ocr_ms_total/t.ocr_calls as f64}else{0.},"item_ocr_calls":t.item_ocr_calls,"item_ocr_ms":if t.item_ocr_calls>0{t.item_ms_total/t.item_ocr_calls as f64}else{0.},"text_cache_hits":t.text_cache_hits,"cycle_avg_ms":if obs>0{busy/obs as f64}else{0.}})
}
fn overlay(a: &mut Value, b: Value) {
    if let Some(map) = b.as_object() {
        for (k, v) in map {
            a[k] = v.clone();
        }
    }
}
fn annotate(im: &mut image::RgbImage, r: core::Rect) {
    for y in r[1].max(0)..(r[1] + r[3]).min(im.height() as i32) {
        for x in r[0].max(0)..(r[0] + r[2]).min(im.width() as i32) {
            if x < r[0] + 2 || x >= r[0] + r[2] - 2 || y < r[1] + 2 || y >= r[1] + r[3] - 2 {
                im.put_pixel(x as u32, y as u32, image::Rgb([255, 255, 0]));
            }
        }
    }
}
fn record(
    app: &App,
    run: &str,
    seq: &mut usize,
    step: &Step,
    frame: Option<&image::RgbImage>,
    tracker: &Tracker,
) -> Result<()> {
    let (Some(kind), Some(s)) = (&step.kind, &step.session) else {
        return Ok(());
    };
    *seq += 1;
    let mut shots = vec![];
    if let Some(frame) = frame {
        let mut panel = core::crop(frame, s.panel_rect);
        let file = format!("{:04}_panel.png", seq);
        panel.save(app.store.root.join(run).join(&file))?;
        shots.push(file);
        if kind == "red" {
            for b in &s.red_blobs {
                let mut r = b.bbox;
                r[0] -= s.panel_rect[0];
                r[1] -= s.panel_rect[1];
                annotate(&mut panel, r);
            }
            let file = format!("{:04}_red_annotated.png", seq);
            panel.save(app.store.root.join(run).join(&file))?;
            shots.push(file);
        }
    }
    let event = json!({"seq":seq,"t":core::now(),"kind":kind,"text":s.ocr_text,"name_bbox":s.name_bbox,"observations":s.observations,"red_count":if kind=="red"{s.red_blobs.len()}else{0},"item_names":s.red_blobs.iter().map(|b|b.ocr_name.clone()).collect::<Vec<_>>(),"streak":tracker.streak,"shots":shots,"start":s.start,"end":s.end,"end_reason":s.end_reason,"panel_rect":s.panel_rect,"red_observed":s.red,"blobs":if kind=="red"{json!(s.red_blobs)}else{json!([])}});
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(app.store.root.join(run).join("events.jsonl"))?;
    writeln!(file, "{}", event)?;
    app.store.append(run, &event)?;
    Ok(())
}
fn work_loop(app: &App, run: &str) -> Result<Value> {
    let cfg = app.config.lock().unwrap().clone();
    let title = app.window.lock().unwrap().clone();
    let window = capture::find(&title)?;
    let roi = cfg
        .work_region
        .or(Some([1. - cfg.right_frac, 0., cfg.right_frac, 1.]));
    let mut tr = Tracker::new(cfg.clone())?;
    let mut observer = observer::Observer::new();
    let mut seq = 0;
    let mut obs = 0;
    let mut busy = 0.;
    let mut snapshot: Option<image::RgbImage> = None;
    let mut red_snapshot: Option<image::RgbImage> = None;
    let mut trace = fs::File::create(app.store.root.join(run).join("trace.jsonl"))?;
    let mut flush = Instant::now();
    let mut update = Instant::now();
    let outcome: Result<()> = (|| {
        while !app.stop.load(Ordering::Relaxed) {
            let begin = Instant::now();
            let rect = capture::region(&window, roi)?;
            if !capture::visible(&window, rect) {
                observer.unavailable();
                if tr.session.is_some() {
                    let step = tr.end("窗口遮挡", true);
                    record(app, run, &mut seq, &step, snapshot.as_ref(), &tr)?;
                    snapshot = None;
                }
                red_snapshot = None;
                let mut state = app.state.lock().unwrap();
                state["foreground"] = json!(false);
                state["current"] = Value::Null;
                state["state"] = json!("IDLE");
                overlay(&mut state, observer.status());
                drop(state);
                thread::sleep(Duration::from_millis(250));
                continue;
            }
            let frame = capture::grab(rect)?;
            let capture_ms = begin.elapsed().as_secs_f64() * 1000.;
            let detect = Instant::now();
            let checked = observer.due();
            if checked {
                check_mode(&window, &mut observer, &tr.engine)?;
            }
            let previous_counts = (tr.total, tr.streak);
            let mut step = if observer.paused() {
                tr.pause(observer.reason())
            } else {
                tr.step(&frame)?
            };
            // Refresh before committing an opening or settlement. A cached normal
            // state must not admit someone else's safe when spectator UI appears.
            if step.kind.is_some() && !checked && !observer.paused() {
                check_mode(&window, &mut observer, &tr.engine)?;
                if observer.paused() {
                    let newly_opened = step.kind.as_deref() == Some("start");
                    if tr.session.is_some() {
                        step = tr.pause(observer.reason());
                    } else if let Some(s) = step.session.as_mut() {
                        // Undo settlement counters; this interrupted observation is excluded.
                        tr.total = previous_counts.0;
                        tr.streak = previous_counts.1;
                        s.end_reason = Some(observer.reason().into());
                        step.kind = Some("incomplete".into());
                        tr.pause(observer.reason());
                    }
                    if newly_opened {
                        step.kind = None;
                        step.session = None;
                    }
                }
            }
            let detect_ms = detect.elapsed().as_secs_f64() * 1000.;
            obs += 1;
            if step.visible {
                snapshot = Some(frame.clone());
            }
            if !step.reds.is_empty() {
                red_snapshot = Some(frame.clone());
            }
            let evidence = if step.kind.as_deref() == Some("red") {
                red_snapshot.as_ref().or(snapshot.as_ref())
            } else {
                snapshot.as_ref()
            };
            record(app, run, &mut seq, &step, evidence, &tr)?;
            if step.kind.as_ref().is_some_and(|k| k != "start") {
                snapshot = None;
                red_snapshot = None;
            }
            let cycle_ms = begin.elapsed().as_secs_f64() * 1000.;
            busy += cycle_ms;
            let mut m = metrics(&tr, obs, busy);
            overlay(&mut m, observer.status());
            {
                let mut s = app.state.lock().unwrap();
                overlay(&mut s, m.clone());
                overlay(&mut s, observer.status());
                overlay(
                    &mut s,
                    json!({"foreground":true,"state":if observer.paused(){observer.state()}else if tr.session.is_some(){"SESSION"}else{"IDLE"},"current":tr.session,"streak":tr.streak,"total":tr.total,"misses":tr.misses,"capture_ms":capture_ms,"detect_ms":detect_ms,"cycle_ms":cycle_ms,"target":window.title}),
                );
            }
            writeln!(
                trace,
                "{}",
                json!({"t":core::now(),"state":if observer.paused(){observer.state()}else if tr.session.is_some(){"SESSION"}else{"IDLE"},"mode":observer.status(),"visible":step.visible,"score":tr.score,"event":step.kind,"candidates":tr.candidates,"capture_ms":capture_ms,"detect_ms":detect_ms,"cycle_ms":cycle_ms,"ocr_calls":tr.ocr_calls})
            )?;
            if flush.elapsed().as_secs() > 2 || step.kind.is_some() {
                trace.flush()?;
                flush = Instant::now();
            }
            if update.elapsed().as_secs() > 5 {
                app.store.update(run, json!({"metrics":m}))?;
                update = Instant::now();
            }
            thread::sleep(Duration::from_secs_f64(if observer.paused() {
                0.25
            } else if tr.session.is_some() {
                cfg.session_interval.max(0.001).min(cfg.interval)
            } else {
                cfg.interval.max(0.02)
            }));
        }
        Ok(())
    })();
    if tr.session.is_some() {
        let step = tr.end(
            if outcome.is_err() {
                "检测异常中断"
            } else {
                "用户停止"
            },
            true,
        );
        record(app, run, &mut seq, &step, snapshot.as_ref(), &tr)?;
    }
    trace.flush()?;
    let mut m = metrics(&tr, obs, busy);
    overlay(&mut m, observer.status());
    overlay(&mut app.state.lock().unwrap(), m.clone());
    outcome?;
    Ok(m)
}
fn start(app: &Arc<App>) -> Result<bool> {
    let env = app.environment.status();
    if env["status"] != "ready" {
        bail!("{}", env["message"].as_str().unwrap_or("环境尚未就绪"));
    }
    let mut state = app.state.lock().unwrap();
    if state["running"] == true {
        return Ok(false);
    }
    let words = keyword_filter::load(&app.base)?;
    app.config.lock().unwrap().red_exclude_words = words;
    let title = app.window.lock().unwrap().clone();
    if title.is_empty() {
        bail!("先选择目标窗口");
    }
    capture::find(&title)?;
    let run = chrono::Local::now().format("%Y%m%d_%H%M%S_%6f").to_string();
    fs::create_dir(app.store.root.join(&run))?;
    let cfg = app.config.lock().unwrap().clone();
    app.store.update(&run,json!({"started_at":core::now(),"stopped_at":null,"status":"running","window":title,"config":cfg,"interval":cfg.interval,"miss_need":cfg.miss_need,"version":env!("CARGO_PKG_VERSION")}))?;
    *state = initial();
    overlay(
        &mut state,
        json!({"running":true,"run_dir":run,"started_at":core::now()}),
    );
    app.stop.store(false, Ordering::Relaxed);
    drop(state);
    let app = app.clone();
    thread::spawn(move || {
        let begin = Instant::now();
        let result = work_loop(&app, &run);
        let mut s = app.state.lock().unwrap();
        s["running"] = json!(false);
        s["current"] = Value::Null;
        s["state"] = json!("IDLE");
        s["stopped_at"] = json!(core::now());
        let (status, error, m) = match result {
            Ok(m) => ("stopped", None, m),
            Err(e) => ("error", Some(e.to_string()), s.clone()),
        };
        s["error"] = json!(error);
        drop(s);
        let _=app.store.update(&run,json!({"status":status,"error":error,"stopped_at":core::now(),"duration_s":begin.elapsed().as_secs_f64(),"metrics":m}));
    });
    Ok(true)
}
fn response(req: Request, bytes: Vec<u8>, mime: &str, status: u16) {
    let r = Response::from_data(bytes)
        .with_status_code(StatusCode(status))
        .with_header(Header::from_bytes("Content-Type", mime).unwrap())
        .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap());
    let _ = req.respond(r);
}
fn query(url: &str, key: &str) -> String {
    url.split_once('?').map_or(String::new(), |(_, q)| {
        q.split('&')
            .find_map(|p| {
                let (k, v) = p.split_once('=')?;
                (k == key).then(|| {
                    percent_encoding::percent_decode_str(v)
                        .decode_utf8_lossy()
                        .to_string()
                })
            })
            .unwrap_or_default()
    })
}
fn body(req: &mut Request) -> Result<Value> {
    let n = req.body_length().unwrap_or(0);
    if n == 0 || n > 65536 {
        bail!("请求大小无效");
    }
    let mut data = String::new();
    req.as_reader().take(65537).read_to_string(&mut data)?;
    if data.len() > 65536 {
        bail!("请求太大");
    }
    Ok(serde_json::from_str(&data)?)
}
fn handle(mut req: Request, app: &Arc<App>) {
    let url = req.url().to_string();
    let path = url.split('?').next().unwrap_or("");
    let method = req.method().as_str().to_string();
    if method == "GET" && path.starts_with("/feedback/") {
        let name = &path[10..];
        let result: Result<fs::File> = (|| {
            if !name.starts_with("feedback-v")
                || !name.ends_with(".zip")
                || name.contains("..")
                || !name
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_' || c == b'.')
            {
                bail!("反馈文件名无效");
            }
            let root = app.base.join("exports").canonicalize()?;
            let file = root.join(name).canonicalize()?;
            if !file.starts_with(&root) {
                bail!("反馈文件越界");
            }
            Ok(fs::File::open(file)?)
        })();
        return match result {
            Ok(file) => {
                let r = Response::from_file(file)
                    .with_header(Header::from_bytes("Content-Type", "application/zip").unwrap())
                    .with_header(
                        Header::from_bytes(
                            "Content-Disposition",
                            format!("attachment; filename=\"{name}\""),
                        )
                        .unwrap(),
                    )
                    .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap());
                let _ = req.respond(r);
            }
            Err(e) => response(
                req,
                json!({"error":e.to_string()}).to_string().into_bytes(),
                "application/json",
                400,
            ),
        };
    }
    if (method == "GET" || method == "POST") && path == "/api/export" {
        let result: Result<PathBuf> = (|| {
            let _guard = app
                .export_lock
                .try_lock()
                .map_err(|_| anyhow::anyhow!("正在打包反馈日志，请稍后重试"))?;
            let mut run = query(&url, "run");
            let state = app.state.lock().unwrap().clone();
            if run.is_empty() {
                run = state["run_dir"].as_str().unwrap_or("").into();
            }
            if run.is_empty() {
                run = app.store.names()?.first().cloned().unwrap_or_default();
            }
            let config = json!(app.config.lock().unwrap().clone());
            feedback::export(
                &app.base,
                &app.store,
                (!run.is_empty()).then_some(run.as_str()),
                state,
                app.environment.status(),
                config,
            )
        })();
        if method == "POST" {
            return match result {
                Ok(path) => {
                    let name = path.file_name().unwrap().to_string_lossy();
                    response(
                        req,
                        json!({"filename":name,"download_url":format!("/feedback/{name}")})
                            .to_string()
                            .into_bytes(),
                        "application/json; charset=utf-8",
                        200,
                    );
                }
                Err(e) => response(
                    req,
                    json!({"error":format!("{e:#}")}).to_string().into_bytes(),
                    "application/json; charset=utf-8",
                    400,
                ),
            };
        }
        return match result {
            Ok(path) => match fs::File::open(&path) {
                Ok(file) => {
                    let name = path.file_name().unwrap().to_string_lossy();
                    let response = Response::from_file(file)
                        .with_header(Header::from_bytes("Content-Type", "application/zip").unwrap())
                        .with_header(
                            Header::from_bytes(
                                "Content-Disposition",
                                format!("attachment; filename=\"{name}\""),
                            )
                            .unwrap(),
                        )
                        .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap());
                    let _ = req.respond(response);
                }
                Err(e) => response(
                    req,
                    json!({"error":e.to_string()}).to_string().into_bytes(),
                    "application/json",
                    400,
                ),
            },
            Err(e) => response(
                req,
                json!({"error":format!("{e:#}")}).to_string().into_bytes(),
                "application/json; charset=utf-8",
                400,
            ),
        };
    }
    if method == "GET" && (path == "/assets/app.css" || path == "/assets/app.js") {
        return if path.ends_with(".css") {
            response(
                req,
                include_bytes!("../assets/app.css").to_vec(),
                "text/css; charset=utf-8",
                200,
            )
        } else {
            response(
                req,
                include_bytes!("../assets/app.js").to_vec(),
                "text/javascript; charset=utf-8",
                200,
            )
        };
    }
    if method == "GET" && (path == "/" || path == "/history") {
        return response(
            req,
            if path == "/" {
                include_bytes!("../assets/index.html").to_vec()
            } else {
                include_bytes!("../assets/history.html").to_vec()
            },
            "text/html; charset=utf-8",
            200,
        );
    }
    let result: Result<Value> = (|| match (method.as_str(), path) {
        ("GET", "/api/environment") => Ok(app.environment.status()),
        ("POST", "/api/environment/retry") => {
            if app.state.lock().unwrap()["running"] == true {
                bail!("先停止检测再检查环境");
            }
            Ok(json!({"ok":true, "started":app.environment.start()}))
        }
        ("GET", "/api/info") => Ok(json!({"version":env!("CARGO_PKG_VERSION")})),
        ("GET", "/api/status") => Ok(app.state.lock().unwrap().clone()),
        ("GET", "/api/stats") => app.store.stats(),
        ("GET", "/api/windows") => {
            Ok(json!({"windows":capture::windows(),"current":app.window.lock().unwrap().clone()}))
        }
        ("GET", "/api/history") => {
            let limit = query(&url, "limit")
                .parse::<usize>()
                .unwrap_or(30)
                .clamp(1, 100);
            let offset = query(&url, "offset").parse::<usize>().unwrap_or(0);
            app.store.list(limit, offset)
        }
        ("GET", "/api/runs") => Ok(json!({"runs":app.store.names()?})),
        ("GET", "/api/run") => app
            .store
            .run(&query(&url, "run"))?
            .ok_or_else(|| anyhow::anyhow!("记录不存在")),
        ("GET", "/api/events") => {
            let mut run = query(&url, "run");
            if run.is_empty() {
                run = app.state.lock().unwrap()["run_dir"]
                    .as_str()
                    .unwrap_or("")
                    .into();
            }
            if run.is_empty() {
                Ok(json!({"events":[],"last_seq":0}))
            } else {
                app.store
                    .events(&run, query(&url, "since").parse().unwrap_or(0))
            }
        }
        ("POST", "/api/window") => {
            if app.state.lock().unwrap()["running"] == true {
                bail!("运行中不能改窗口");
            }
            let b = body(&mut req)?;
            let title = b["title"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("窗口标题无效"))?;
            *app.window.lock().unwrap() = title.into();
            let mut settings: Value = fs::read(app.base.join("settings.json"))
                .ok()
                .and_then(|s| serde_json::from_slice(&s).ok())
                .unwrap_or(json!({}));
            settings["window"] = json!(title);
            fs::write(
                app.base.join("settings.json"),
                serde_json::to_vec_pretty(&settings)?,
            )?;
            Ok(json!({"ok":true,"window":title}))
        }
        ("POST", "/api/start") => Ok(json!({"ok":true,"started":start(app)?})),
        ("POST", "/api/stop") => {
            app.stop.store(true, Ordering::Relaxed);
            Ok(json!({"ok":true}))
        }
        ("POST", "/api/quit") => {
            app.stop.store(true, Ordering::Relaxed);
            let app = app.clone();
            thread::spawn(move || {
                while app.state.lock().unwrap()["running"] == true {
                    thread::sleep(Duration::from_millis(100));
                }
                thread::sleep(Duration::from_millis(200));
                std::process::exit(0);
            });
            Ok(json!({"ok":true}))
        }
        ("POST", "/api/review") => {
            let b = body(&mut req)?;
            app.store.review(
                b["run"].as_str().unwrap_or(""),
                b["seq"]
                    .as_i64()
                    .ok_or_else(|| anyhow::anyhow!("事件编号错误"))?,
                if b["label"].is_null() {
                    None
                } else {
                    Some(
                        b["label"]
                            .as_str()
                            .ok_or_else(|| anyhow::anyhow!("复核标签错误"))?,
                    )
                },
            )?;
            Ok(json!({"ok":true}))
        }
        _ => bail!("页面不存在"),
    })();
    if method == "GET" && (path.starts_with("/shot/") || path == "/api/preview") {
        let image: Result<Vec<u8>> = (|| {
            if path.starts_with("/shot/") {
                let decoded = percent_encoding::percent_decode_str(&path[6..]).decode_utf8_lossy();
                let (run, file) = decoded
                    .split_once('/')
                    .ok_or_else(|| anyhow::anyhow!("截图路径错误"))?;
                if !storage::valid_run(run)
                    || !file.ends_with(".png")
                    || file.contains('/')
                    || file.contains('\\')
                    || file.contains("..")
                {
                    bail!("截图路径错误");
                }
                let base = app.store.root.canonicalize()?;
                let p = base.join(run).join(file).canonicalize()?;
                if !p.starts_with(&base) {
                    bail!("截图路径错误");
                }
                Ok(fs::read(p)?)
            } else {
                let title = query(&url, "w");
                let w = capture::find(&title)?;
                let mut im = capture::grab(capture::region(&w, None)?)?;
                let cfg = app.config.lock().unwrap().clone();
                let r = core::work_rect(&cfg, im.width() as i32, im.height() as i32);
                annotate(&mut im, r);
                let mut data = Cursor::new(vec![]);
                im.write_to(&mut data, image::ImageFormat::Png)?;
                Ok(data.into_inner())
            }
        })();
        return match image {
            Ok(data) => response(req, data, "image/png", 200),
            Err(e) => response(
                req,
                json!({"error":e.to_string()}).to_string().into_bytes(),
                "application/json; charset=utf-8",
                400,
            ),
        };
    }
    match result {
        Ok(v) => response(
            req,
            v.to_string().into_bytes(),
            "application/json; charset=utf-8",
            200,
        ),
        Err(e) => response(
            req,
            json!({"error":e.to_string(),"ok":false})
                .to_string()
                .into_bytes(),
            "application/json; charset=utf-8",
            400,
        ),
    }
}
fn cli(args: &[String]) -> Result<bool> {
    if args
        .get(1)
        .is_some_and(|a| a == "--panel" || a == "--analyze" || a == "--replay")
    {
        let mode = &args[1];
        let path = args.get(2).ok_or_else(|| anyhow::anyhow!("缺少输入文件"))?;
        let mut cfg = Config::default();
        let base = args
            .iter()
            .position(|a| a == "--data-dir")
            .and_then(|i| args.get(i + 1))
            .map(PathBuf::from)
            .unwrap_or(std::env::current_exe()?.parent().unwrap().into());
        cfg.red_exclude_words = keyword_filter::load(&base)?;
        cfg.ocr_interval = 0.;
        let mut t = Tracker::new(cfg.clone())?;
        if mode == "--panel" {
            let anchor: Vec<i32> = args
                .get(3)
                .ok_or_else(|| anyhow::anyhow!("缺少框坐标"))?
                .split(',')
                .map(str::parse)
                .collect::<std::result::Result<_, _>>()?;
            if anchor.len() != 4 {
                bail!("框应为 x,y,w,h");
            }
            let im = image::open(path)?.to_rgb8();
            let mut red = vec![];
            for _ in 0..3 {
                red = t.scan(&im, anchor.clone().try_into().unwrap())?;
            }
            println!("{}", json!({"red":red,"candidates":t.candidates}));
        } else {
            let files: Vec<String> = if mode == "--replay" {
                serde_json::from_slice(&fs::read(path)?)?
            } else {
                vec![path.clone(); 4]
            };
            let mut events = vec![];
            let mut observer = observer::Observer::new();
            for file in files {
                let im = image::open(&file)?.to_rgb8();
                observer.check(&t.engine, &observer::Observer::footer(&im))?;
                let work = core::crop(
                    &im,
                    core::work_rect(&cfg, im.width() as i32, im.height() as i32),
                );
                let s = if observer.paused() {
                    t.pause(observer.reason())
                } else {
                    t.step(&work)?
                };
                if s.kind.is_some() {
                    events.push(json!({"file":file,"kind":s.kind,"session":s.session}));
                }
            }
            println!(
                "{}",
                json!({"events":events,"current":t.session,"candidates":t.candidates,"total":t.total,"mode":observer.status()})
            );
        }
        return Ok(true);
    }
    Ok(false)
}
fn run() -> Result<()> {
    // Keep the MTA alive across environment checks and successive detection workers.
    // Cached WinRT factories become invalid if the last initialized thread exits.
    let _runtime = runtime::Apartment::new()?;
    capture::dpi();
    let args: Vec<String> = std::env::args().collect();
    if cli(&args)? {
        return Ok(());
    }
    let base = args
        .iter()
        .position(|a| a == "--data-dir")
        .and_then(|i| args.get(i + 1))
        .map(PathBuf::from)
        .unwrap_or(std::env::current_exe()?.parent().unwrap().into());
    fs::create_dir_all(&base)?;
    // Create the editable file on first launch; malformed files are reported on Start.
    let _ = keyword_filter::load(&base);
    let settings: Value = fs::read(base.join("settings.json"))
        .ok()
        .and_then(|s| serde_json::from_slice(&s).ok())
        .unwrap_or(json!({}));
    let mut cfg = Config::default();
    if let Some(r) = settings.get("work_region") {
        cfg.work_region = serde_json::from_value(r.clone()).ok();
    }
    let app = Arc::new(App {
        environment: environment::Environment::new(),
        store: storage::Store::new(&base.join("runs"))?,
        state: Mutex::new(initial()),
        config: Mutex::new(cfg),
        window: Mutex::new(settings["window"].as_str().unwrap_or("").into()),
        stop: AtomicBool::new(false),
        base,
        export_lock: Mutex::new(()),
    });
    let first = args
        .iter()
        .position(|a| a == "--port")
        .and_then(|i| args.get(i + 1))
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(17933);
    let mut chosen = None;
    for port in first..first.saturating_add(10) {
        if let Ok(server) = Server::http(format!("127.0.0.1:{port}")) {
            chosen = Some((server, port));
            break;
        }
    }
    let (server, port) = chosen.ok_or_else(|| anyhow::anyhow!("没有可用的本地端口"))?;
    let url = format!("http://127.0.0.1:{port}");
    println!("{url}");
    app.environment.start();
    if !args.iter().any(|a| a == "--no-browser") {
        let _ = std::process::Command::new("explorer.exe").arg(&url).spawn();
    }
    for req in server.incoming_requests() {
        let app = app.clone();
        thread::spawn(move || handle(req, &app));
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        let text = format!("暗区红品检测启动失败：{e}");
        eprintln!("{text}");
        unsafe {
            let _ = windows::Win32::UI::WindowsAndMessaging::MessageBoxW(
                None,
                &windows::core::HSTRING::from(text),
                &windows::core::HSTRING::from("暗区红品检测"),
                windows::Win32::UI::WindowsAndMessaging::MB_OK,
            );
        }
    }
}
