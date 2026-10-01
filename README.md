# 暗区红品检测

Windows 屏幕识别工具，用于识别暗区突围保险箱中的红色稀有度物品。提供 Python v0.2.1 与 Rust 原生 v0.3.1；网页控制检测，SQLite 持久化运行历史和人工复核。

## 运行 Python 源码

需要 Windows 10/11 x64、Python 3.13。

```powershell
python -m pip install -r requirements.txt
python main.py --no-pick serve
```

启动会检查简体中文 OCR，缺少时发起系统组件安装，需要联网并允许 Windows 管理员授权。安装失败可在网页重试。源码版需要先安装 requirements.txt 中的 Python 调用库；已发布的便携版内置运行时和调用库。

选择目标窗口，检查预览后开始检测。数据保存在 runs/，设置保存在 settings.json。窗口被遮挡时暂停，未完成的保险箱不计入出红率。统计覆盖所有红色稀有度物品，每次完整打开计一次，暂不去重；人工复核独立保存。

## 构建 Rust

需要 Rust stable 与 Visual Studio MSVC C++ 构建工具。

```powershell
cargo test --locked --manifest-path native/Cargo.toml
cargo build --release --locked --manifest-path native/Cargo.toml
```

程序位于 native/target/release/ab-red-detect.exe，网页和安装逻辑内嵌。发布程序不需要 Python 或 Rust。修改网页后运行 `python tools/export_assets.py` 更新嵌入资源。

## 测试

```powershell
python -m unittest discover -s tests -p "test_*.py"
python test_real.py
python tests/native_qa.py
```

系统 OCR 缺失、授权取消、安装失败及需要重启的测试使用模拟系统操作，不卸载或安装本机组件。实际 OCR 回归需要已有中文 OCR。

## 版本目录

| 目录 | 来源 |
| --- | --- |
| version/v0.2.0 | 原 Python v0.2.0 发布版源码快照 |
| version/v0.2.1 | Python v0.2.1 发布版源码快照 |
| version/v0.3.0 | 移除环境补丁后的行为恢复源码；非原二进制的精确原始快照 |
| version/v0.3.1 | 当前 Rust v0.3.1 源码、网页和环境准备代码 |

根目录是当前双实现开发源码。docs/ 保留检测与环境验证说明；tests/fixtures/ 保留必要游戏回归样本。运行历史、个人窗口设置、依赖缓存、解释器和编译产物不属于此源码归档。
