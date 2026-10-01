# Rust v0.3.0 行为恢复源码

原 v0.3.0 没有独立保存 Rust 源码快照。本目录从 v0.3.1 移除环境检测补丁，并以 v0.2.0 原始网页恢复原生界面；不得称作原发布二进制的精确原始源码快照。

原始 v0.3.0 发布程序仍保留在桌面原目录。恢复源码的构建和测试结果见仓库验证说明。构建：`cargo build --release --locked --manifest-path native/Cargo.toml`。
