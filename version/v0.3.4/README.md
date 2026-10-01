# Rust v0.3.4 源码

来自此次已验证原生源码与发布包。修复 OCR 运行时生命周期导致的访问异常崩溃；包含真实窗口连续启停回归、独立网页、最佳连续十箱与反馈 ZIP。检测判据沿用 v0.3.2。Python v0.2.2 暂时废弃，后续只维护 Rust。完整行为、配置与验证边界见仓库根 README.md 和 docs/v0.3.4验证.md。

在本目录执行 cargo test --locked --manifest-path native/Cargo.toml 和 cargo build --release --locked --manifest-path native/Cargo.toml。
