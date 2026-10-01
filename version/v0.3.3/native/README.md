# 暗区红品检测 v0.3.3

Windows x64 原生程序。截图使用 Windows GDI；中文 OCR 使用系统 Windows OCR；SQLite 和 PNG 编码内置。网页资源直接嵌入 exe。发布版不需要安装 Python、Rust、OpenCV 或 Visual C++ 运行库。

双击 exe 打开本地网页，自动检测简体中文 OCR。缺少时通过 Windows 管理员授权安装 Basic 和 OCR 系统组件，并验证引擎可用；网页显示准备状态、错误及重试入口。已有 OCR 时不安装、不提权。安装要求联网，若系统要求重启则重启后重试。安装器提权，检测程序保持普通权限。停止结束当前运行；退出程序保存记录并关闭后台进程。关闭网页只关闭界面。

数据在 exe 同目录的 `runs`：SQLite 保存运行元数据、事件与人工复核；每次运行子目录保留 PNG、events.jsonl、trace.jsonl。旧版 JSONL 启动时自动导入。运行开始/结束时间、持续时间、已结算/未完成数量、出红率都保留。出红率仅统计自动判定的完整结算记录，人工复核单独保存，不覆盖原判定。

设置来自同目录 settings.json：`window` 为窗口标题，`work_region` 可填写相对客户区的 [x,y,w,h] 比例；未设置时使用右侧 40%。当前不做同箱去重。

开发：安装 Rust stable 和 MSVC 构建工具后，在工程根目录执行 `cargo test --manifest-path native/Cargo.toml`、`cargo build --release --locked --manifest-path native/Cargo.toml`，也可运行 build.ps1。根目录 .cargo/config.toml 启用静态 CRT。构建输出在 native/target/release/ab-red-detect.exe。

诊断命令：

```powershell
ab-red-detect.exe --analyze full-frame.png
ab-red-detect.exe --panel panel.png 37,42,105,21
ab-red-detect.exe --replay frames.json
ab-red-detect.exe --data-dir test-data --port 17945 --no-browser
```

`frames.json` 是按时间排序的图片路径数组。诊断模式输出 JSON，可用管道重定向保存。Rust 的相关性匹配采用局部采样，形态学为纯 Rust 实现；与 Python 并非逐像素完全相同，必须运行真实截图和播放回归后再发布。

屏蔽词配置为 exe（或 --data-dir）目录的 `red_filter.json`：`{"exclude_words":["实验室","行星"]}`。第一次只运行 exe 时会生成默认文件，每次点击开始重读，运行中保持词表不变；修改后停止并重新开始。空数组可关闭屏蔽，非法 JSON/字段/词语会阻止开始，并保留原文件。检测按 OCR 文本子串匹配；不屏蔽“航天”，防止误杀航天导航仪。诊断命令也读取相同目录的规则。

从 v0.3.3 起仅维护 Rust，Python v0.2.2 暂时废弃并冻结。Rust 网页独立放在 `native/assets/index.html`、`history.html`、`app.css`、`app.js`，四个资源嵌入 exe。修改后重新构建即可；不再从 Python 网页生成资源。

`stats.rs` 用十箱滑动窗口计算个人最高爆率。按运行 ID 和事件序号读取完整历史，只纳入 red / clean；start 不占箱数，incomplete 和运行切换清空窗口。每个窗口必须恰好十个完整结算保险箱，出红率为出红箱数乘 10%；并列取最近的窗口，不足十箱返回空。该统计按自动判定，独立人工复核不修改成绩。页面显示十个结果格，点击链接筛选对应十条结算记录。

`feedback.rs` 导出单次运行的 ZIP：数据库一致快照 `run.json`、原始事件与逐帧日志、被事件引用的 PNG、环境与当前配置、个人成绩、缺失文件列表。不会复制整个 SQLite；没有历史时输出环境诊断。JSON / 日志用快速 Deflate，PNG 直接存储；64 KiB 分块处理并用 CRC32 校验。采用 ZIP32，过大文件明确报错。写入 `.zip.part`，成功同步后改名，失败删除未完成文件。同一时间只进行一个导出。

`POST /api/export?run=<id>` 返回文件名和下载地址；`GET /feedback/<filename>` 流式下载已经生成的 ZIP，`GET /api/export` 为直接生成并下载接口。未指定 run 时选当前运行，其次最新历史，否则仅导出环境。`GET /api/stats` 返回 best_ten、settled、longest_segment、window_size。导出结果保留在 exe 或 --data-dir 下的 exports；运行中导出的原始日志可能比一致快照更晚，也可能有未完成末行。

前端使用本地 HTML/CSS/原生 JS，无在线资源。包括运行与环境状态、指标卡、历史筛选、截图弹窗、人工复核、导出反馈、页面与按钮过渡、窄屏布局；遵循 prefers-reduced-motion。
