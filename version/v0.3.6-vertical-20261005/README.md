# Rust v0.3.6 竖三格完整框修复源码

2026-10-05，在两格修复之后补足实测面板高度及保存证据范围。初版和两格快照均保留。

运行 cargo test --locked --manifest-path native/Cargo.toml 和 cargo build --release --locked --manifest-path native/Cargo.toml；开发环境安装 Pillow 后运行 python tests/vertical_slots_regression.py 和 python tests/two_slots_regression.py。实际窗口运行 python tests/native_observer_qa.py --vertical-three。私人反馈测试默认跳过，不携带私人图片。详细行为见仓库根 docs/v0.3.6竖三格完整框修复.md。
