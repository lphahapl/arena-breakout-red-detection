# 暗区红品检测 v0.3.2

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
