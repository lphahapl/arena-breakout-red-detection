# Rust v0.3.6 两格漏报修复重发源码

2026-10-05，保留版本号并更新 v0.3.6 发布标签。修复横向和竖向两格背景因格线面积漏报；原首次发布快照在 ../v0.3.6。

运行 cargo test --locked --manifest-path native/Cargo.toml 和 cargo build --release --locked --manifest-path native/Cargo.toml。开发者安装 Pillow 后可运行 python tests/two_slots_regression.py；私人反馈测试默认跳过。详细行为见仓库根 docs/v0.3.6两格修复重发.md。
