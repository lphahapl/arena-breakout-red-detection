"""
容器 UI 检测 + 红色物品色块检测。

流程（三层，逐层短路）：

  第 0 层  裁掉左侧背包区：只保留画面右侧 right_frac 比例。
           暗区里背包占左边约 0.6、搜索容器在右边约 0.4，
           而背包里也可能有红品 —— 直接不看左边就不会被干扰。

  第 1 层  几何筛选：在右侧区域内用「边缘密度 → 连通域 → 最大块」
           找疑似格栅面板，检查它的面积占比 / 长宽比 / 填充率。
           这一层能把全屏 UI（大地图）和居中小框（设置菜单）筛掉。

  第 2 层  周期性确认：在候选面板内对边缘投影做自相关，
           横竖都必须出现显著周期峰（= 规则格子）。
           既是身份确认，又顺便量出了格子像素尺寸（= UI 缩放系数）。

  第 3 层  红色检测：在面板内做 HSV 红色掩码 → 连通域 →
           按「相对面积 / 实心度 / 长宽比 / 色相纯度」筛出候选色块。

所有中间结果都随结果返回，demo 会把它们全部落盘用于 debug。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import cv2
import numpy as np


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class Config:
    # --- 第 0 层：工作区 ---
    # 只看屏幕右侧这个比例（左 0.6 是背包，会带红品干扰）。
    # 有 work_region 时以它为准，right_frac 只作为没框选时的默认值。
    right_frac: float = 0.40
    # 用户手动画的工作区，归一化比例 (fx, fy, fw, fh)，相对窗口客户区。
    # None = 用 right_frac 的默认值。
    # 手动画比固定右 40% 准得多：区域小一个数量级，OCR 和网格检测都跟着省。
    work_region: tuple | None = None
    # 抓屏时已经只抓了工作区，所以帧本身就是工作区 —— work_rect 直接返回整帧。
    # 用于「只抓子区域」那个 2.1 倍的提速（见 capture.Grabber 的 crop 参数）。
    frame_is_work: bool = False

    # --- 第 1 层：面板几何 ---
    canny_lo: int = 40
    canny_hi: int = 120
    dens_win: int = 31          # 边缘密度统计窗口
    dens_thr: float = 0.06      # 密度阈值：高于此值算「纹理密集」
    close_win: int = 21         # 闭运算，把格子连成一整片
    min_grid_side: int = 60     # 面板边长下限（像素）
    grid_extent_min: float = 0.40   # 填充率 = 连通域面积 / 外接矩形面积
    grid_aspect: tuple = (0.45, 2.4)
    max_grid_candidates: int = 6    # 依次尝试前几个连通域
    # 把候选框收紧到「密度持续高」的矩形区所需的最小行/列覆盖率。
    # 密度 → 闭运算 → 连通域会把相邻杂物（图标、角色轮廓）粘进来，
    # 实测把 255x255 的真网格撑成 333x320，周期性被稀释到测不出来。
    refine_cover_min: float = 0.5

    # --- 第 2 层：周期性 ---
    period_lo: int = 24         # 格子间距搜索范围（像素）
    period_hi: int = 320
    period_min_corr: float = 0.22
    # 面板边长 / 周期 必须接近整数，否则说明测出来的不是真的格子间距
    cell_count_range: tuple = (2.0, 22.0)   # 横竖各有多少格
    cell_residual_max: float = 0.18         # 允许的非整数残差比例

    # --- 第 3 层：红色 ---
    # 实测标定：红品槽位底色 = #331410 (R51 G20 B16)，极均匀(通道 std≈2)。
    #
    # 三个用真实截图验证过的结论：
    #   1. 不能用 HSV 色相区分。这个红很暗(V≈51)，色相噪声极大；而金/棕品
    #      底色 #352512 (R53 G37 B18) 的 V 是 53，色相只差 13。
    #      但两者 R/G 比差了近一倍 —— 红品 2.55 vs 金棕品 1.43。这才是本质判据。
    #   2. 不能用 hue_std 过滤。掩码包含物品图案和白色名称的过渡像素，
    #      真红品的 hue_std 高达 19.7，比红色海报还高，会被误杀。
    #   3. 最强判据是「腐蚀后核心区精确匹配该色值的比例」：
    #      真红品 0.83，红色海报 0.00。
    red_mode: str = "hue"                   # "hue"（推荐）| "ratio"（旧，按单一色值标定）
    # --- "hue" 模式：色相 + G≈B ---
    # 红品槽位底色是**半透明**的：压暗背景上呈深红 #331410，压亮背景上呈粉红
    # #A27877。两者色相几乎一样（H=3 vs H=1），但亮度差了 3 倍 ——
    # 所以固定色值匹配是错的方向，必须用色相。
    #
    # G≈B 这一条用来把红和金色/棕色分开：
    #   红品   #A27877 -> G=120 B=119  差 0.8%   通过
    #   红品   #331410 -> G=20  B=16   差 20%    通过
    #   金棕品 #352512 -> G=37  B=18   差 51%    排除
    #   面板棕底 #5B3722 -> G=55 B=34  差 38%    排除
    red_hue_max: int = 9                    # 色相上界（红跨 0，另一段见下）
    red_hue_wrap_min: int = 170             # 色相回绕段下界
    # 饱和度下限。**这条是从一次真误报里挖出来的**：
    # 容器面板在「搜索中」状态时没有物品，但面板是半透明的，会把背后的
    # 暗红场景透出来 —— 实测那块 V=55~69、S 只有 45~88，形状还很大，
    # 亮度判据（真红品 V=71，误报 V=69）根本分不开。
    # 真红品的 S 是 68~159，卡 65 能把两边分开：
    #   误报（搜索中面板底色）S≈45~88 的中低段，真红品 S≥68
    # 别设更高 —— 75 会把「目标定位模块」和「黄金面具(选中)」一起误杀。
    red_sat_min: int = 65
    # 亮度下限。用来挡住**面板透出的场景底色** —— 容器面板是半透明的，
    # 压在暗红场景上时整片都是暗红（实测 V=46~51）。
    # 真红品实测 V=70 / 121 / 162（同一种红在不同光照下差异很大），
    # 所以卡 60：比场景底色（≤51）高，比最暗的真红品（70）低。
    # 踩过：一度设成 100（只量到两个亮的样本就定了），把 V=70 的真红品挡掉了。
    red_val_min: int = 60
    red_gb_ratio_max: float = 0.28          # |G-B| / max(G,B) 上限
    # Bright selected slots can become pale pink in desktop capture (moon sample).
    # Keep the dark-scene threshold; only rescue bright, nearly neutral red hues.
    red_highlight_val_min: int = 180
    red_highlight_sat_min: int = 25
    red_highlight_gb_max: float = 0.08

    # --- 尺寸下限：大红至少占两个格子 ---
    # 用户给的游戏知识：**大红物品至少占两个格子**。
    # 所以一个只占 1 格甚至更小的红色区域，多半是**物品图标上的红色部件**
    # 或者面板透出的场景底色，不是红品。
    #
    # 格子尺寸不直接可得（网格检测已拿掉），但可以从**容器名字的高度**推 ——
    # 实测名字高 23px 时格子约 90px，比值约 3.9。取 3.0 留余量。
    cell_from_name_h: float = 3.0
    # 要求宽、高各至少一个格子，面积至少两个格子
    red_min_cells_side: float = 1.0
    red_min_cells_area: float = 2.0
    # **尺寸上限**：一个物品占不了太多格子。实测那条 575px 宽（8.7 格）的
    # 误报就是靠这个挡掉的 —— 面板透出的场景底色可能横跨一大片，
    # 而真大金最多 3~4 格。4 是实测值（5 也能挡，留 4 更紧）。
    red_max_cells_side: float = 4.0

    # 红品必须**连续这么多轮**都看到才算数。
    # 为什么需要：红品判定只在会话内跑，但会话可能"假装还活着" ——
    # 模板匹配分数 >= tpl_match_min 就算面板还在，而场景画面偶尔也能凑到。
    # 实测有过：面板早没了，会话还开着，于是去扫了一片建筑/天空的场景，
    # 把一小块红色判成了红品。
    #
    # 真实红品会在面板里停留好几秒（十几轮），而运动中的场景误报一帧一个样，
    # 连续两轮都命中的概率很低。这是从机制上堵，而不是碰巧挡住。
    # 判据：**最近 red_confirm_window 轮里有 red_confirm_rounds 轮命中**。
    # 不是"连续 N 轮" —— 实测踩过：视频里红品只被检出**一轮**（下一轮就没了，
    # 可能是物品图案/面板状态变化导致那一帧的掩码不合格），要求连续两轮
    # 直接把真红品漏掉了。
    # 用滑动窗口既能容忍单轮闪断，又能挡住偶发的场景误报。
    red_confirm_rounds: int = 2
    # 确认窗口比确认轮数宽一些。实测踩过：真大金在视频里只有约 0.4 秒
    # 能被检出（同一件物品不同帧之间检测是飘的），按 0.3s/轮算只够 1~2 轮，
    # 卡在"3 轮中 2 轮"的临界点，实时跑就会漏。放宽到 5 轮容得下这种闪断。
    red_confirm_window: int = 5
    # 和容器名字标签的重叠比例上限。标签底色也是红的（实测 #D29285，色相 5），
    # 和红品同色系，只能靠位置排除。
    red_label_overlap_max: float = 0.05

    # --- 用物品名做最后一道判据（简单粗暴但有效）---
    # 对每个红块**再 OCR 它自己那一小块**：
    #   * 命中 red_exclude_words -> 排除。这些物品**图案本身就是红色**
    #     （航天实验室、行星之子），红来自图案不是槽位底，不是大金。
    #   * 一个字都读不到 -> 排除。真大金的槽位上有**物品名文字**，
    #     而面板透出的场景底色上没有文字 —— 这一条正好把场景误报挡掉。
    #   用子串匹配（"实验室"、"行星"），不依赖完整物品名。
    # **用短词**。实测「航天实验室」的 OCR 结果是「航天实验」——少了"室"字，
    # 拿"实验室"去匹配会漏。用户说的"通配两个字"是对的：取最有辨识度的两字。
    red_exclude_words: tuple = ("航天", "行星")
    # **默认关**。「一个字都读不到就排除」听起来能挡场景误报，
    # 但实测是亏的：它杀掉 3/7 个真大金（古董茶壶、耐美拉彩蛋、黄金面具
    # 的槽位 OCR 读不出名字），而场景类误报本来就已经被颜色/尺寸判据挡完了。
    # 对比：开 4/7 正 + 5/5 负，关 7/7 正 + 5/5 负。
    # 真正有用的是上面的 red_exclude_words。
    red_require_text: bool = False
    # 闭运算核。红底被物品图案和文字打洞、甚至**从中间劈成两半**
    # （实测「目标定位模块」的亮金属机体把红底切成左右两块，间距 20px），
    # 9 跨不过去。25 能合上，而 35 会把无关区域也粘起来（负样本全误报）。
    red_close_win: int = 25
    # 把同一槽位被物品图案切开的红块**合并**再判定。
    # 实测：大金的红底会被物品图案（亮金属机体，V≈213 S≈46）从中间劈成左右两半，
    # 间距约 20px，闭运算跨不过去（加大核会把无关区域也粘起来）。
    # 做法：先按这个半径膨胀掩码找连通域（=一个槽位），
    # 再用**原始掩码**在这个区域内统计面积/实心度。
    # **默认关闭**。想法是对的方向（同一个槽位被物品图案切开的红块应该合并），
    # 但实测没有可用的半径：桥接 20px 的断口需要 35px 膨胀，而那个半径会把
    # 无关区域也粘起来 —— 负样本全部误报、历史误报从 1 涨到 8。
    # 20px 的断口靠膨胀跨不过去，只能靠"槽位是格子"这个结构信息来定，
    # 那是下一步的事，不是调参能解决的。
    red_merge_px: int = 0

    # --- "ratio" 模式（旧）：按安全箱里量到的 #331410 精确匹配 ---
    # 留着是因为它对"同一个界面内、背景光照固定"的场景更精确。
    # 但对局内的半透明面板不适用 —— 那边底色会漂。
    red_target_bgr: tuple = (16, 20, 51)    # #331410
    red_r_min: int = 32
    red_r_max: int = 120
    red_rg_min: float = 2.0                 # R/G：红品 2.55，金棕品 1.43
    red_rb_min: float = 1.8
    red_core_erode: int = 5
    red_core_match_tol: int = 10
    red_core_match_min: float = 0.55

    # 形状（次要判据，比核心匹配率弱）
    red_area_frac_min: float = 0.0025       # 相对「面板面积」的占比，不是绝对像素
    # 实心度 = 像素数 / 外接矩形面积。**别设高** ——
    # 物品图案会把红底中间掏空，图案越大的物品实心度越低：
    # 实测黄金面具 0.57（图案小）、目标定位模块 0.42（图案几乎占满整格）。
    # 一度设 0.45，正好把后者挡掉了。
    # 0.30 是实测最优点，但**必须看检出的到底是哪个物品**再定：
    # 视频2 t=23.5 那个 0.345 的块是「耐美拉彩蛋」（真大金），不是旁边的
    # 「集邮册」（金品）。我一度误读成后者、把阈值撤回，反而漏掉了真大金。
    # 矩阵（extent=0.30）：5 个正样本 5/5、4 个负样本 0 误报、
    # 历史 23 个误报仍只剩 1 个真红品。降到 0.25 历史误报就回来了（1->2）。
    # 「细长条」有 aspect 管，「小碎块」有 size 管，这条不是必需的守门员。
    red_extent_min: float = 0.30
    red_aspect: tuple = (0.35, 2.8)

    # --- 容器类型识别（OCR 容器名字）---
    # 光看「搜索面板出现了」数不出保险箱 —— 搜的容器不都是保险箱。
    # 面板里会显示容器名字，OCR 它就能只数目标容器。
    # 用子串匹配，所以「钛金安全箱」也能被「安全箱」命中。
    container_keyword: str = "保险箱"
    # 容器名字是**短标签**，命中的那一行不该太长。
    # 实测踩过：工具把自己的网页标题「暗区·保险箱出红记录」当成了容器名
    # （标题里含"保险箱"），开出一堆假会话。已把标题改掉，这里再加一道防线 ——
    # 其他 UI 文字、视频标题里也可能含这三个字。
    # 已知容器名：保险箱 / 电子保险箱 / 机密保险箱 / 钥匙保险箱，都不超过 8 字。
    container_name_max_len: int = 8
    # OCR 的放大倍数，**跑两遍取并集**。
    #
    # 为什么不能只跑一遍：实测两边各有盲区，而且互补 ——
    #   1.0x 漏「保险箱」（实测 t=92/t=96 两帧漏掉，而漏掉名字 = 整条链路
    #        根本不开会话，红品再准也检不到，这是用户报的"出红没探测到"的根因）
    #   1.5x 漏「机密保险箱」（sample2/sample3 两帧）
    # 并集在 7 个正样本上 7/7，7 个负样本上 0 误报。
    #
    # 代价：耗时约 103ms -> 262ms。IDLE 阶段 1 秒一次 = 单核 26%。
    # 正确性优先 —— 漏检红品比多占点 CPU 严重得多。
    # 想省 CPU 可以只留 (1.0,) 或 (1.5,) 单遍，但会漏上面说的那两类。
    ocr_upscales: tuple = (1.0, 1.5)
    # 单遍放大倍数。name / snap 这类诊断命令用它（跑一遍就够，
    # 人要看得清全部识别结果，不需要 track 那种召回率优先的两遍并集）。
    ocr_upscale: float = 1.0
    ocr_lang: str = "zh-Hans-CN"
    # OCR 单独限频，不和帧率绑死。
    # 实测右 0.4 全量 OCR 要 143ms（768x1080，1.0+1.5 两个倍数）。
    # **它直接决定「容器开多久才被认出来」**：最坏检测延迟 ≈
    # ocr_interval + 单次 OCR 耗时。1.0 时最坏 1.14 秒 —— 容器开得短就漏了。
    # 0.5 把最坏延迟压到 0.64 秒，代价是占空比 14% -> 29%。
    # 命中路径做了惰性放大倍数（1.0 有命中就不跑 1.5），实际更省。
    # 设 0 = 不限频（每轮都跑，调参时用）。
    ocr_interval: float = 0.5
    # After learning the label position, probe that small area every 0.2s.
    # Still scan the entire work area periodically to recover moved panels.
    ocr_full_interval: float = 1.0
    # Short hover/transfer frames need more samples while a safe is visible.
    # Idle still uses the runner's normal interval and cropped OCR probes.
    session_interval: float = 0.01
    # --- 红品搜索范围 ---
    # 红品必须在**容器面板**里，而面板就在 OCR 认出的名字标签正下方。
    # 不框住的话会误报场景里的红色东西 —— 实测踩过：木质地板被当成红品，
    # 而且它在画面最底部，把存下来的截图拉成 282x1327 的废条。
    # 倍数相对**名字高**，这样不同分辨率/UI 缩放下等比缩放。
    # 实测名字 110x23 时，面板约 x 0..350、y 130..500。
    # 横向要**给足**：实测真实面板约 470px 宽而名字只有 110px 宽，
    # 设 6 时算出的范围右边界 386 会擦着红品格子（202..385）过去，
    # 差一点就把红品切掉。设 16 才稳。
    # 真正挡住场景误报的是**纵向**边界，横向放宽不影响。
    panel_pad_x_ratio: float = 16.0     # 左右各扩 名字高 * 16
    panel_pad_top_ratio: float = 2.0    # 上方扩 名字高 * 2
    panel_pad_bot_ratio: float = 15.0   # 下方扩 名字高 * 15

    # --- 面板存活判定（会话内）---
    # **不用 OCR**：实测小区域 OCR 极不稳 —— 同一个「机密保险箱」被读成过
    # 「密保险箱」「也保險粕」「机密保殓霜」，命中率在 0/3 到 3/3 之间飘。
    # 拿它判「名字还在不在」会把一个容器拆成好几条记录。
    #
    # 改用模板匹配：容器名字标签是静态 UI 元素，位置固定、外观稳定。
    # 实测同一帧 NCC=1.0000，空帧 0.0000，判别力拉满，而且只要 ~1ms。
    # 存活判定用**迟滞**，不要单一阈值：
    #   >= tpl_match_min  -> 还活着
    #   <  tpl_gone_max   -> 确定没了（才计入 miss）
    #   中间是灰区          -> 两条都不动
    # 踩过：单一阈值下，模板分数抖几帧到阈值以下就判"消失"，
    # 一个容器被拆成好几次记录（实测有一次只活 2.6 秒，紧接着 1.8 秒后又开一次）。
    tpl_match_min: float = 0.65
    tpl_gone_max: float = 0.35
    # 灰区最多待几轮。**必须有这个上限** —— 实测踩过：面板已经消失了，
    # 但分数正好停在灰区（实测 0.352~0.407），于是一轮轮"既不算活着也不算消失"，
    # 会话被永久卡住、永远不结算，出红事件也就永远发不出来。
    # 灰区是为了容忍几帧抖动，不是让人长期住下的。
    tpl_gray_max_rounds: int = 5
    tpl_search_pad: int = 30          # 搜索窗口在模板位置外扩多少像素

    # 已经锚定到名字位置时用的间隔。
    #
    # 设 0（不限频）是有意的：这时只扫名字周围一小块（实测 ~10ms，
    # 见 track.Tracker.anchor_region），限频省不下什么，却会**拖长会话结束的判定**。
    # 会话结束靠「名字连续 N 轮消失」，而轮与轮之间至少要隔一个限频周期 ——
    # 踩过：原来设 0.2s，配上 0.5s 的轮询间隔，关掉一个容器要 1.5s 才判定结束，
    # 期间开下一个箱子就会被并进同一个会话，导致「开了好几个只记了一条」。
    ocr_interval_fast: float = 0.0
    # 只 OCR 这个子区域，**归一化比例** (fx, fy, fw, fh)，相对工作区左上角。
    # None = 整个工作区。
    #
    # 这是省 OCR 开销最有效的一招：实测全量 143.6ms，而名字那一小块
    # （190x70）只要 9.0ms —— 15.9 倍。
    # 不能用缩放代替：缩到 50% 虽快 2.6 倍，但识别行数从 20 掉到 5。
    #
    # 注意留白：Windows OCR 对裁剪留白极敏感，文字高 15px 时上下留白
    # <25px 会直接返回空。跑 `name` 命令它会给出算好留白的建议值。
    ocr_region: tuple | None = None

    # red_mode="hsv" 时才用


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def period_candidates(profile: np.ndarray, lo: int, hi: int,
                      min_corr: float) -> list[tuple[int, float]]:
    """
    自相关找所有显著周期峰，返回 [(period_px, corr), ...]，按周期升序。

    注意这里只是「候选」，不能直接取最大峰：实测中格子间距 90px 的图，
    自相关在 74px（= 格子宽，相邻格子边框对齐）处也有强峰，直接取最大峰
    会测错。真正的判别在 pick_period 里做。
    """
    p = profile.astype(np.float64)
    p = p - p.mean()
    if p.size < 2 * lo or not np.any(p):
        return []

    ac = np.correlate(p, p, mode="full")[p.size - 1:]
    ac = ac / (ac[0] + 1e-9)

    peaks: list[tuple[int, float]] = []
    upper = min(hi, ac.size - 1)
    for i in range(max(lo, 1), upper):
        if ac[i] > ac[i - 1] and ac[i] >= ac[i + 1] and ac[i] > min_corr:
            peaks.append((i, float(ac[i])))
    return sorted(peaks)


def fold_score(profile: np.ndarray, period: int, nbin: int = 32) -> float:
    """
    把一维投影按 period 折叠到 nbin 个相位箱，返回「箱内方差 / 总方差」。

    这是判别真伪周期的关键：
      - 真周期：每次折叠都落在同一相位，箱内值高度一致 -> 分数趋近 0
      - 假周期（比如把 90 的间距当成 74）：相位每轮漂移，箱内被抹平 -> 分数接近 1

    实测能把 74 和 90 明确分开，而单看自相关强度分不开。
    """
    prof = np.asarray(profile, dtype=np.float64)
    n = prof.size
    if n < period * 2:
        return 1e9
    total = prof.var()
    if total <= 1e-12:
        return 1e9

    phase = np.arange(n) % period
    idx = (phase * nbin) // period
    within, cnt = 0.0, 0
    for b in range(nbin):
        v = prof[idx == b]
        if v.size > 1:
            within += v.var() * v.size
            cnt += v.size
    if cnt == 0:
        return 1e9
    return float((within / cnt) / total)


def pick_period(profile: np.ndarray, span: int, cfg: "Config"):
    """
    在候选周期里选出真正的格子间距。

    评分顺序（前一项优先）：
      1. fold_score  —— 折叠一致性，真周期趋近 0。这是主判据
      2. 格子数残差  —— span/period 应当接近整数
      3. 自相关强度  —— 打平手时用

    返回 (period_px, corr, fold)；选不出返回 (None, 0.0, None)。
    """
    cands = period_candidates(profile, cfg.period_lo, cfg.period_hi, cfg.period_min_corr)
    if not cands:
        return None, 0.0, None

    strongest = max(c for _, c in cands)
    lo_c, hi_c = cfg.cell_count_range
    best = None
    for p, c in cands:
        if c < 0.45 * strongest:          # 太弱的候选直接丢
            continue
        n = span / p
        if not (lo_c <= n <= hi_c):
            continue
        residual = abs(n - round(n)) / n
        fs = fold_score(profile, p)
        key = (round(fs, 3), round(residual, 3), -c)
        if best is None or key < best[0]:
            best = (key, p, c, fs)

    if best is None:
        return None, 0.0, None
    return best[1], best[2], best[3]


def longest_run(mask: np.ndarray) -> tuple[int, int]:
    """返回 mask 中最长连续 True 段的 [start, end) 下标。全 False 时返回 (0, 0)。"""
    best_s = best_e = 0
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start > best_e - best_s:
                best_s, best_e = start, i
            start = None
    if start is not None and len(mask) - start > best_e - best_s:
        best_s, best_e = start, len(mask)
    return best_s, best_e


def profile_image(profile: np.ndarray, height: int = 110) -> np.ndarray:
    """把一维投影画成曲线图，方便肉眼 debug 周期性。"""
    p = np.asarray(profile, dtype=np.float64)
    if p.size == 0:
        return np.zeros((height, 1, 3), np.uint8)
    p = p - p.min()
    if p.max() > 0:
        p = p / p.max()
    img = np.zeros((height, p.size, 3), np.uint8)
    pts = np.array([(i, int((1.0 - p[i]) * (height - 1))) for i in range(p.size)],
                   dtype=np.int32)
    cv2.polylines(img, [pts], False, (0, 255, 0), 1, cv2.LINE_AA)
    return img


def red_mask(bgr: np.ndarray, cfg: Config, exclude: tuple | None = None) -> np.ndarray:
    if cfg.red_mode == "ratio":
        return _red_mask_ratio(bgr, cfg, exclude)
    return _red_mask_hue(bgr, cfg, exclude)


def _clear_excluded(mask: np.ndarray, exclude: tuple | None) -> None:
    if exclude is None:
        return
    x, y, w, h = (int(v) for v in exclude)
    x1, y1 = max(x, 0), max(y, 0)
    x2, y2 = min(x + w, mask.shape[1]), min(y + h, mask.shape[0])
    if x2 > x1 and y2 > y1:
        mask[y1:y2, x1:x2] = 0


def _red_mask_hue(bgr: np.ndarray, cfg: Config, exclude: tuple | None = None) -> np.ndarray:
    """
    色相判据（默认）。

    能同时覆盖深红底 #331410 和粉红底 #A27877 —— 这两者是对局外/对局内
    同一种红品底色，因为槽位底是半透明的，随背后场景明暗变化。

    额外加一条 G≈B：红色是 R 远大于 G 且 G 与 B 接近；
    金色/棕色则 G 明显大于 B。这一条把金品和面板棕底挡在外面。
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0]
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]

    # 只用到 G 和 B —— R 不参与判定，色相已经把 R 是最大通道这件事管住了
    b = bgr[:, :, 0].astype(np.float32)
    g = bgr[:, :, 1].astype(np.float32)

    hue_ok = (h <= cfg.red_hue_max) | (h >= cfg.red_hue_wrap_min)
    gb = np.abs(g - b) / np.maximum(np.maximum(g, b), 1.0)

    normal = (hue_ok & (s >= cfg.red_sat_min) & (v >= cfg.red_val_min)
              & (gb <= cfg.red_gb_ratio_max))
    highlight = (((h <= min(cfg.red_hue_max, 6)) | (h >= max(cfg.red_hue_wrap_min, 174)))
                 & (s >= cfg.red_highlight_sat_min) & (v >= cfg.red_highlight_val_min)
                 & (gb <= cfg.red_highlight_gb_max))
    m = (normal | highlight).astype(np.uint8) * 255

    # Remove the red container label before closing can bridge it to an item.
    _clear_excluded(m, exclude)
    if cfg.red_close_win > 1:
        k = np.ones((cfg.red_close_win, cfg.red_close_win), np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    _clear_excluded(m, exclude)
    return m


def _red_mask_ratio(bgr: np.ndarray, cfg: Config, exclude: tuple | None = None) -> np.ndarray:
    """
    按实测色值 #331410 标定的红色掩码。

    用 R/G、R/B 比值而不是 HSV 色相：这个红很暗(V≈51)，色相噪声极大；
    而 R/G 比对亮度不敏感，且能把红品(2.55)和金棕品(1.43)干净分开。
    """
    b = bgr[:, :, 0].astype(np.float32)
    g = bgr[:, :, 1].astype(np.float32)
    r = bgr[:, :, 2].astype(np.float32)
    m = ((r > cfg.red_r_min) & (r < cfg.red_r_max)
         & (r / (g + 1.0) > cfg.red_rg_min)
         & (r / (b + 1.0) > cfg.red_rb_min)).astype(np.uint8) * 255

    _clear_excluded(m, exclude)
    if cfg.red_close_win > 1:
        # 用 CLOSE 而不是 OPEN：红底被物品图案和白色名称打了很多洞，
        # CLOSE 把洞填上；OPEN 会把红底本身腐蚀掉。
        k = np.ones((cfg.red_close_win, cfg.red_close_win), np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    _clear_excluded(m, exclude)
    return m


def _core_match_frac(bgr: np.ndarray, sel: np.ndarray, cfg: Config) -> float:
    """
    腐蚀掉边缘过渡像素后，核心区里精确匹配红品色值的比例。

    这是最强判据（实测：真红品 0.83，红色海报 0.00）。
    必须先腐蚀：物品图案和白色名称的过渡像素本身不是红底，
    它们的颜色介于红和白之间，会拉低匹配率。
    """
    k = np.ones((cfg.red_core_erode, cfg.red_core_erode), np.uint8)
    core = cv2.erode(sel.astype(np.uint8), k) > 0
    if int(core.sum()) < 20:
        return 0.0
    px = bgr[core].astype(np.int16)
    tgt = np.array(cfg.red_target_bgr, np.int16)
    return float((np.abs(px - tgt).max(axis=1) <= cfg.red_core_match_tol).mean())


# --------------------------------------------------------------------------
# 第 1~2 层：找容器面板
# --------------------------------------------------------------------------
def find_grid(region_bgr: np.ndarray, cfg: Config) -> dict:
    """
    在 region 内找规则格子面板。

    返回 dict:
      found   : bool
      rect    : (x, y, w, h)  相对 region 左上角
      period  : (px, py)      格子间距
      debug   : {edges, density, hot, col_profile, row_profile, reason}
    """
    dbg: dict = {"reason": ""}

    gray = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, cfg.canny_lo, cfg.canny_hi)
    dbg["edges"] = edges

    density = cv2.boxFilter((edges > 0).astype(np.float32), -1,
                            (cfg.dens_win, cfg.dens_win))
    dbg["density"] = (np.clip(density, 0, 1) * 255).astype(np.uint8)

    hot = (density > cfg.dens_thr).astype(np.uint8) * 255
    hot = cv2.morphologyEx(hot, cv2.MORPH_CLOSE,
                           np.ones((cfg.close_win, cfg.close_win), np.uint8))
    dbg["hot"] = hot

    n, _, stats, _ = cv2.connectedComponentsWithStats(hot, 8)
    if n <= 1:
        dbg["reason"] = "区域内没有纹理密集区"
        return {"found": False, "rect": None, "period": (None, None), "debug": dbg}

    # 按面积从大到小依次试前 N 个连通域。
    # 只取最大块是不够的：实测「角色轮廓 + 面板」会粘成一个脏的大连通域，
    # 把真正干净的格子面板挤到第二名。
    order = sorted(range(1, n), key=lambda i: -int(stats[i, cv2.CC_STAT_AREA]))
    order = order[:cfg.max_grid_candidates]

    attempts = []
    for idx in order:
        info = _check_candidate(edges, hot, stats, idx, cfg, dbg)
        attempts.append(info)
        if info.get("ok"):
            dbg.update({k: v for k, v in info.items() if k != "reason"})
            dbg["attempts"] = attempts
            dbg["reason"] = "OK"
            return {"found": True, "rect": info["rect"],
                    "period": info["period"], "debug": dbg}

    dbg["attempts"] = attempts
    if attempts:
        first = attempts[0]
        a = first.get("aspect")
        dbg["rect"] = first.get("rect")
        dbg["area"] = first.get("area")
        dbg["extent"] = first.get("extent")
        dbg["aspect"] = a
        dbg["reason"] = (f"试了 {len(attempts)} 个候选都不合格；"
                         f"最大块的拒绝原因: {first.get('reason')}")
    else:
        dbg["reason"] = "没有合格尺寸的连通域"
    return {"found": False, "rect": None, "period": (None, None), "debug": dbg}


def _check_candidate(edges: np.ndarray, hot: np.ndarray, stats: np.ndarray,
                     idx: int, cfg: Config, dbg: dict) -> dict:
    """对单个连通域跑完整判定链。返回 info dict（含 ok / reason）。"""
    x, y, w, h = (int(stats[idx, cv2.CC_STAT_LEFT]), int(stats[idx, cv2.CC_STAT_TOP]),
                  int(stats[idx, cv2.CC_STAT_WIDTH]), int(stats[idx, cv2.CC_STAT_HEIGHT]))
    area = int(stats[idx, cv2.CC_STAT_AREA])
    extent = float(area / float(w * h)) if w and h else 0.0
    aspect = float(w / h) if h else 0.0
    info: dict = {"rect": (x, y, w, h), "area": area,
                  "extent": round(extent, 4), "aspect": round(aspect, 3)}

    if w < cfg.min_grid_side or h < cfg.min_grid_side:
        info["reason"] = f"面板太小 {w}x{h} < {cfg.min_grid_side}"
        return info
    if extent < cfg.grid_extent_min:
        info["reason"] = f"填充率过低 {extent:.2f}（形状不规则，不像面板）"
        return info
    if not (cfg.grid_aspect[0] < aspect < cfg.grid_aspect[1]):
        info["reason"] = f"长宽比 {aspect:.2f} 不在 {cfg.grid_aspect}"
        return info

    # --- 收紧候选框到「密度持续高」的矩形区 ---
    # 连通域会把相邻杂物粘进来。实测：真实网格 255x255，CC 给出的却是
    # (0,0,333,320)，把左上角一个图标包了进去，周期性因此被稀释到测不出来。
    # 按行/列的「热像素覆盖率」取最长连续段，就能切回真正的网格矩形。
    sub_hot = hot[y:y + h, x:x + w] > 0
    x1, x2 = longest_run(sub_hot.mean(axis=0) >= cfg.refine_cover_min)
    y1, y2 = longest_run(sub_hot.mean(axis=1) >= cfg.refine_cover_min)
    if x2 - x1 >= cfg.min_grid_side and y2 - y1 >= cfg.min_grid_side:
        info["rect_raw"] = (x, y, w, h)
        x, y, w, h = x + x1, y + y1, x2 - x1, y2 - y1
        info["rect"] = (x, y, w, h)
        info["aspect"] = round(w / h, 3) if h else 0.0
        if not (cfg.grid_aspect[0] < info["aspect"] < cfg.grid_aspect[1]):
            info["reason"] = f"收紧后长宽比 {info['aspect']:.2f} 不在 {cfg.grid_aspect}"
            return info

    # --- 周期性确认 ---
    sub_edges = edges[y:y + h, x:x + w]
    col_profile = sub_edges.sum(axis=0).astype(np.float64)   # 竖直边分布 -> 格子间距
    row_profile = sub_edges.sum(axis=1).astype(np.float64)   # 水平边分布 -> 格子间距

    # 抹掉面板外框的影响，只留内部周期
    if col_profile.size > 8:
        col_profile = col_profile.copy()
        col_profile[:4] = col_profile[-4:] = 0
    if row_profile.size > 8:
        row_profile = row_profile.copy()
        row_profile[:4] = row_profile[-4:] = 0

    # 这两个 profile 只在该候选成功时才写进总 debug（否则会被下一个候选覆盖）
    px, cx, fx = pick_period(col_profile, w, cfg)
    py, cy, fy = pick_period(row_profile, h, cfg)
    info["period"] = (px, py)
    info["corr"] = (round(cx, 3), round(cy, 3))
    info["fold"] = (None if fx is None else round(fx, 3),
                    None if fy is None else round(fy, 3))

    if px is None or py is None:
        info["reason"] = (f"没测出规则格子 (x={px}, corr={cx:.2f}, fold={fx}; "
                          f"y={py}, corr={cy:.2f}, fold={fy})")
        return info

    # --- 格子数必须是整数 ---
    n_cols, n_rows = w / px, h / py
    info["cells"] = (round(n_cols, 2), round(n_rows, 2))
    res_c = abs(n_cols - round(n_cols)) / n_cols
    res_r = abs(n_rows - round(n_rows)) / n_rows
    info["cell_residual"] = (round(res_c, 3), round(res_r, 3))
    if res_c > cfg.cell_residual_max or res_r > cfg.cell_residual_max:
        info["reason"] = (f"格子数非整数残差过大 ({res_c:.2f}, {res_r:.2f}) "
                          f"> {cfg.cell_residual_max}")
        return info

    info["ok"] = True
    info["reason"] = "OK"
    info["col_profile"] = col_profile
    info["row_profile"] = row_profile
    return info


# --------------------------------------------------------------------------
# 第 3 层：红色色块
# --------------------------------------------------------------------------
def find_red_blobs(panel_bgr: np.ndarray, cfg: Config,
                   exclude: tuple | None = None,
                   name_h: int | None = None) -> dict:
    """
    在面板内找符合红品特征的色块。

    exclude: 要排除的矩形 (x, y, w, h)，相对 panel_bgr。
             用来排掉**容器名字标签** —— 它的底色也是红的（实测 #D29285 色相 5、
             #9C4B3F 色相 4），和红品同色系，靠颜色分不开。传 OCR 给的名字
             bbox（留过余量）进来即可。

    返回 dict: {mask, blobs: [...], area_panel: int}
    每个 blob 带全部判据的实测值 + 拒绝原因，方便 debug。
    """
    h, w = panel_bgr.shape[:2]
    panel_area = float(h * w)
    mask = red_mask(panel_bgr, cfg, exclude)

    # 先膨胀再找连通域：这样被物品图案切开的红块会归到同一个"槽位"
    if cfg.red_merge_px > 1:
        k = np.ones((cfg.red_merge_px, cfg.red_merge_px), np.uint8)
        grown = cv2.dilate(mask, k)
    else:
        grown = mask
    n, labels, stats, _ = cv2.connectedComponentsWithStats(grown, 8)

    blobs: list[dict] = []
    for i in range(1, n):
        bx, by, bw, bh = (int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP]),
                          int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT]))
        # 面积用**原始掩码**在这个连通域里数，不是膨胀后的
        sel = (labels[by:by + bh, bx:bx + bw] == i) & (mask[by:by + bh, bx:bx + bw] > 0)
        area = int(sel.sum())
        if area == 0:
            continue

        area_frac = area / panel_area
        extent = area / float(bw * bh) if bw and bh else 0.0
        aspect = bw / float(bh) if bh else 0.0

        record = {
            "bbox": (bx, by, bw, bh),
            "area": area,
            "area_frac": round(area_frac, 5),
            "extent": round(extent, 3),
            "aspect": round(aspect, 3),
        }
        checks = {
            "area_frac": (area_frac, cfg.red_area_frac_min, area_frac >= cfg.red_area_frac_min),
            "extent": (extent, cfg.red_extent_min, extent >= cfg.red_extent_min),
            "aspect": (aspect, cfg.red_aspect,
                       cfg.red_aspect[0] < aspect < cfg.red_aspect[1]),
        }

        if cfg.red_mode == "ratio":
            cm = _core_match_frac(panel_bgr[by:by + bh, bx:bx + bw], sel, cfg)
            record["core_match"] = round(cm, 3)
            checks["core"] = (cm, cfg.red_core_match_min, cm >= cfg.red_core_match_min)

        # 位置排除：容器名字标签
        if exclude is not None:
            ex, ey, ew, eh = exclude
            ix, iy = max(bx, ex), max(by, ey)
            ix2, iy2 = min(bx + bw, ex + ew), min(by + bh, ey + eh)
            ov = max(ix2 - ix, 0) * max(iy2 - iy, 0)
            frac = ov / float(area) if area else 0.0
            cx, cy = bx + bw / 2.0, by + bh / 2.0
            center_in = (ex <= cx <= ex + ew) and (ey <= cy <= ey + eh)
            record["label_overlap"] = round(frac, 3)
            record["label_center_in"] = center_in
            # **只看中心在不在标签框里**，不看重叠比例。
            # 实测踩过：膨胀合并（close=25）后红块会往标签方向延伸，
            # 重叠比例轻易超过 0.05，把真大金误杀（黄金面具 / 古董茶壶都中招）。
            # 而中心判据本来就够干净 —— 标签的中心在框内，物品槽位的中心远在其下。
            checks["label"] = (frac, cfg.red_label_overlap_max, not center_in)

        if name_h:
            cell = max(name_h * cfg.cell_from_name_h, 1.0)
            side_ok = (bw >= cell * cfg.red_min_cells_side
                       and bh >= cell * cfg.red_min_cells_side)
            bbox_area = bw * bh
            need = (cell ** 2) * cfg.red_min_cells_area
            area_ok = bbox_area >= need
            record["cell_px"] = round(cell, 1)
            record["bbox_area"] = bbox_area
            too_wide = (bw > cell * cfg.red_max_cells_side
                        or bh > cell * cfg.red_max_cells_side)
            checks["size"] = ((bw, bh, bbox_area), (cell, cell, need),
                              side_ok and area_ok and not too_wide)

        reasons = [k for k, (_, _, ok) in checks.items() if not ok]
        record["accepted"] = not reasons
        record["reject"] = reasons
        blobs.append(record)

    blobs.sort(key=lambda b: (not b["accepted"], -b["area"]))
    return {"mask": mask, "blobs": blobs, "area_panel": int(panel_area)}


# --------------------------------------------------------------------------
# 总入口
# --------------------------------------------------------------------------
def analyze(frame_bgr: np.ndarray, cfg: Config) -> dict:
    """
    对整帧做一次分析。

    返回 dict，关键字段：
      ui_found  : bool          有没有检测到容器面板
      red_found : bool          面板内有没有符合条件的红品色块
      accepted  : list          命中的色块（坐标是相对整帧的绝对像素）
      debug     : dict          所有中间图（BGR uint8），demo 直接 imwrite
    """
    H, W = frame_bgr.shape[:2]
    x0 = int(round(W * (1.0 - cfg.right_frac)))
    x0 = max(0, min(x0, W - 1))
    right = frame_bgr[:, x0:]
    right_abs = (x0, 0, W - x0, H)   # 相对整帧的绝对矩形

    res: dict = {
        "frame_size": (W, H),
        "right_abs": right_abs,
        "right_frac": cfg.right_frac,
        "ui_found": False,
        "red_found": False,
        "accepted": [],
        "grid": None,
        "debug": {"00_full": frame_bgr, "01_right": right.copy()},
    }

    grid = find_grid(right, cfg)
    gd = grid["debug"]
    if "edges" in gd:
        res["debug"]["02_edges"] = gd["edges"]
    if "density" in gd:
        res["debug"]["03_density"] = gd["density"]
    if "hot" in gd:
        res["debug"]["04_hot"] = gd["hot"]
    if "col_profile" in gd:
        res["debug"]["06_col_profile"] = profile_image(gd["col_profile"])
    if "row_profile" in gd:
        res["debug"]["07_row_profile"] = profile_image(gd["row_profile"])

    # 面板叠加图（不管成不成功都画，成功画绿、失败画红）
    overlay = right.copy()
    if gd.get("rect"):
        gx, gy, gw, gh = gd["rect"]
        color = (0, 255, 0) if grid["found"] else (0, 0, 255)
        cv2.rectangle(overlay, (gx, gy), (gx + gw, gy + gh), color, 2)
        cv2.putText(overlay, f"{gw}x{gh} ext={gd['extent']:.2f}", (gx, max(gy - 6, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    res["debug"]["05_grid"] = overlay
    res["grid"] = {
        "found": grid["found"],
        "rect_rel_right": gd.get("rect"),
        "period": grid["period"],
        "cells": gd.get("cells"),
        "cell_residual": gd.get("cell_residual"),
        "extent": gd.get("extent"),
        "aspect": gd.get("aspect"),
        "reason": gd.get("reason"),
    }

    if not grid["found"]:
        res["debug"]["09_candidates"] = overlay
        return res

    # --- 有面板了，进去找红色 ---
    gx, gy, gw, gh = gd["rect"]
    panel = right[gy:gy + gh, gx:gx + gw].copy()
    blobs = find_red_blobs(panel, cfg)
    res["debug"]["08_redmask"] = blobs["mask"]

    cand = panel.copy()
    accepted_abs = []
    for b in blobs["blobs"]:
        bx, by, bw, bh = b["bbox"]
        if b["accepted"]:
            cv2.rectangle(cand, (bx, by), (bx + bw, by + bh), (0, 255, 255), 2)
            # 色块在整帧里的绝对坐标
            accepted_abs.append((
                right_abs[0] + gx + bx, right_abs[1] + gy + by, bw, bh
            ))
        else:
            cv2.rectangle(cand, (bx, by), (bx + bw, by + bh), (128, 128, 128), 1)
            cv2.putText(cand, ",".join(r[:3] for r in b["reject"]),
                        (bx, max(by - 4, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (128, 128, 128), 1, cv2.LINE_AA)

    res["debug"]["09_candidates"] = cand
    res["debug"]["10_panel"] = panel
    res["ui_found"] = True
    res["red_found"] = len(accepted_abs) > 0
    res["accepted"] = accepted_abs
    res["blobs"] = blobs["blobs"]
    res["panel_abs"] = (right_abs[0] + gx, right_abs[1] + gy, gw, gh)
    return res


def config_to_dict(cfg: Config) -> dict:
    d = asdict(cfg)
    d["grid_aspect"] = list(cfg.grid_aspect)
    d["red_aspect"] = list(cfg.red_aspect)
    return d
