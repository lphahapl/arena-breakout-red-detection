# Python 版本暂时废弃

从 Rust v0.3.3 开始，后续只维护 Rust 实现。Python 的最后版本为 v0.2.2，暂时标记为废弃，不再新增功能、修复或重新打包发布。

根目录 Python 应用源码和 `version/v0.2.x/` 快照保留用于查看历史实现；旧 Release 和本机 Python 测试目录继续保留。它们没有 v0.3.3 的反馈导出、连续十箱成绩和新网页。

后续程序修改放在 `native/`，新版本仅归档和发布 Rust。网页资源直接维护 `native/assets/`，旧 Python 页面生成器已退役。开发验证可继续使用 Python 标准库驱动 Rust exe；这不构成更新 Python 应用，也不增加发行版运行依赖。

升级时先停止旧程序，再将其完整 `runs/` 目录复制到 Rust exe 同目录；Python 便携版对应 `app/runs/`。SQLite 应在正常停止后复制，或使用数据库备份接口；不要仅复制一个仍在写入的 SQLite 主文件。Rust 启动会导入旧 JSONL 并保留独立人工复核。不要覆盖旧目录中的原始证据。
