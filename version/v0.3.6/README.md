# Rust v0.3.6 源码

对应长笛、滑板误判修复和观战/结算暂停记录的正式发布。网页、默认规则、截图与 OCR、历史统计及反馈导出随原生程序构建。Python 已冻结，此快照只维护 Rust。

在本目录运行 cargo test --locked --manifest-path native/Cargo.toml 和 cargo build --release --locked --manifest-path native/Cargo.toml。cargo test 的私有反馈用例默认跳过，不携带私人图片；公开面板和合成回归保留。实际窗口验证可运行 tests/native_observer_qa.py、native_live_qa.py、native_features_qa.py，需要开发 Python/Tk 和中文 Windows OCR。

详细判据、业务行为和升级边界见仓库根 README.md、docs/v0.3.6验证.md。
