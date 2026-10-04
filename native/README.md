# 暗区红品检测 v0.3.5

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

屏蔽词配置为 exe（或 --data-dir）目录的 `red_filter.json`：`{"exclude_words":["实验室","行星","唱片机"]}`。第一次只运行 exe 时会生成默认文件，每次点击开始重读，运行中保持词表不变；修改后停止并重新开始。空数组可关闭屏蔽，非法 JSON/字段/词语会阻止开始，并保留原文件。检测按 OCR 文本子串匹配；不屏蔽“航天”，防止误杀航天导航仪。诊断命令也读取相同目录的规则。

从 v0.3.3 起仅维护 Rust，Python v0.2.2 暂时废弃并冻结。Rust 网页独立放在 `native/assets/index.html`、`history.html`、`app.css`、`app.js`，四个资源嵌入 exe。修改后重新构建即可；不再从 Python 网页生成资源。

`stats.rs` 用十箱滑动窗口计算个人最高爆率。按运行 ID 和事件序号读取完整历史，只纳入 red / clean；start 不占箱数，incomplete 和运行切换清空窗口。每个窗口必须恰好十个完整结算保险箱，出红率为出红箱数乘 10%；并列取最近的窗口，不足十箱返回空。该统计按自动判定，独立人工复核不修改成绩。页面显示十个结果格，点击链接筛选对应十条结算记录。

`feedback.rs` 导出单次运行的 ZIP：数据库一致快照 `run.json`、原始事件与逐帧日志、被事件引用的 PNG、环境与当前配置、个人成绩、缺失文件列表。不会复制整个 SQLite；没有历史时输出环境诊断。JSON / 日志用快速 Deflate，PNG 直接存储；64 KiB 分块处理并用 CRC32 校验。采用 ZIP32，过大文件明确报错。写入 `.zip.part`，成功同步后改名，失败删除未完成文件。同一时间只进行一个导出。

`POST /api/export?run=<id>` 返回文件名和下载地址；`GET /feedback/<filename>` 流式下载已经生成的 ZIP，`GET /api/export` 为直接生成并下载接口。未指定 run 时选当前运行，其次最新历史，否则仅导出环境。`GET /api/stats` 返回 best_ten、settled、longest_segment、window_size。导出结果保留在 exe 或 --data-dir 下的 exports；运行中导出的原始日志可能比一致快照更晚，也可能有未完成末行。

前端使用本地 HTML/CSS/原生 JS，无在线资源。包括运行与环境状态、指标卡、历史筛选、截图弹窗、人工复核、导出反馈、页面与按钮过渡、窄屏布局；遵循 prefers-reduced-motion。


v0.3.4 修复真实开始检测时的访问异常：主线程的 WinRT MTA 初始化覆盖整个程序生命周期，避免短暂环境检查线程结束后缓存语言工厂失效。每个 OCR Engine 使用线程绑定的 Apartment 守卫，先释放 OCR 对象，再配对 RoUninitialize；守卫不能跨线程移动。新增 tests/native_live_qa.py，验证准备线程结束后实际截图/OCR、连续三轮启停、环境重试及并发状态/历史请求。旧 v0.3.3 在同一测试下报 0xc0000005，修复版通过。网页断连时改为中文说明，连接恢复后清除该提示。


v0.3.5 修复三类反馈误报：标题必须以保险箱关键词结尾并排除等待/打开等动作文字，且下方必须找到至少三条等距长网格竖线。网格边长从实际截图测量并按会话锚点缓存；候选至少占两格，单格柯恩币拖动提示不满足尺寸条件。暗色普通分支 V<100 时限 H≤6 或 H≥174，排除笔记本电脑棕底的 H=8–9；明亮暖红与高亮分支保留。新增合成网格/颜色回归；真实反馈截图回归留在本地，通过 AB_RED_PRIVATE_FIXTURES 指定目录并运行 cargo test --manifest-path native/Cargo.toml feedback_false_positives -- --ignored。旧记录与复核不自动改写。


词表修补：按反馈将唱片机暂时加入 JSON 排除词，此次不重新打包或发布新版本，仅匹配“唱片机”，不扩大为“唱片”。同一进程每次开始检测重新加载 JSON；运行中保留启动快照。沿用旧自定义 JSON 时不会自动覆盖，需手动追加该词。真实唱片机截图保留在本地私有回归中，公开测试验证默认词条、其它红品名称放行及空词表关闭规则。


2026-10-05 main 开发修补（未打包）：普通红底默认 S≥55、V≥40；高亮默认 H≤9/H≥170、S≥20、V≥180，并保留 G/B 差与暗色暖色相限制。候选宽高至少 0.9 格、面积至少两格、宽高比 0.22～4.5；缓存格子边长与起点，要求四边至少三边对齐网格（允许提示截断一边），横线不可用时要求左右两边对齐。支持暗色和选中长笛，排除不与槽位对齐的红色滑板拖动图标。具体结果见 docs/长笛滑板与观战状态修复验证.md。

observer.rs 使用整个目标客户区底部中央 [0.20,0.88,0.60,0.12]，与保险箱工作区独立。约每 0.5 秒检查，原尺寸无关键词时再放大 1.5 倍，Engine 按系统 MaxImageDimension 限制最终缩放。任一命中“观战”或“结算”立即暂停，连续两次无命中恢复；遮挡不能当作无提示。启动先检查；开箱/自然结算提交前额外检查本轮未检查的状态，防止旧缓存写入观战事件。已有观察被打断标记 incomplete，total/streak 恢复到打断前；期间跳过物品检测且不创建新开箱事件。

状态 API 增加 play_state、play_state_text、recording_paused、mode_keywords、mode_ocr_calls、mode_ocr_ms；CHECKING/SPECTATING/SETTLEMENT 均暂停。页面显示暂停原因及状态 OCR 耗时；trace 和运行 metrics 保存对应诊断信息。--analyze / --replay 输入为完整游戏截图并应用同一门控，--panel 只诊断裁剪面板，不检查观战。tests/native_observer_qa.py 通过独立的 Tk 游戏画面验证真实截图、中文 OCR、状态切换与持久化统计。

正式 Release 仍为 v0.3.5，已有 exe 和 version/ 历史快照未修改。上述变更只有重新构建 main 才生效，不能通过 red_filter.json 更新。Python 源码仍冻结。
