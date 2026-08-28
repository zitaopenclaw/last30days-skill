# HANDOFF: 为 last30days skill 增加中文搜索源

> 交接文档（2026-08-18）。接手者：GitHub Copilot / OpenCode+Deepseek / 任何 coding agent。
> 本文档是继续工作的完整上下文。工作仓库 = 本文件所在目录。

## 0. TL;DR

给 last30days skill（v3.21.0）新增三个默认启用的中文数据源：**Bilibili、V2EX、掘金**。
三个源模块**已经写好并经过真实 API 验证**，剩下的是**接线（wiring）+ planner 改造 + 文档 + 端到端验收**。
当前分支：`feat/chinese-sources`（基于 main @ v3.21.0, commit c7460f6）。

## 1. 已定稿的设计决策（不要再改，除非有硬理由）

| 决策点 | 结论 |
|---|---|
| 使用场景 | 研究全球（英文）话题时也要中文社区视角 → 中文源**默认启用**，不走 INCLUDE_SOURCES opt-in |
| 查询桥接 | 非中文话题由 **planner 生成 1–2 条中文子查询**，只发往中文源（品牌词如 "Claude Code" 在中文社区本来就用英文，但概念型话题必须翻译） |
| v1 源范围 | Bilibili（直连官方 Web 搜索 API，WBI 签名，免登录）、V2EX（经 sov2ex 社区搜索 API）、掘金（juejin.cn web 搜索 API） |
| 知乎 | **不做独立模块**。planner/模型对中文话题的 web 搜索追加 `site:zhihu.com` 查询（brave backend） |
| 微博 | 不做（反爬太重） |
| 小红书 | 代码已内置但**暂不启用**（依赖本地 xpzouying/xiaohongshu-mcp 浏览器会话服务，太重）。顺手修它的 footer 统计行缺失 bug |
| 新闻站点（36氪/机器之心/量子位） | 不做独立源，web 搜索 site: 过滤覆盖 |
| 工作流 | git fork 工作流，本仓库即工作区；**先自用不提 PR**，故不写 changelog.d、不碰版本号 |
| 验收标准 | 真实查询端到端：英文话题一次 + 中文话题一次 + 引擎 mock 模式测试 |

## 2. 环境拓扑（重要，别搞混）

- **工作仓库**：`D:\zita\kimicode\last30days-skill`（branch `feat/chinese-sources`）
- **运行时加载路径**（junction 链，已全部验证读通写通）：
  - `~/.agents/skills/last30days` → junction → `D:\zita\kimicode\last30days-skill\skills\last30days`（权威入口）
  - `~/.kimi-code/skills/last30days`、`~/.claude/skills/last30days`、`~/.codex/skills/last30days`、`~/.cloud/skills/last30days`（opencode 的 skills.path）全部 junction 到 `~/.agents/skills/last30days`
  - **因此：改仓库里的文件，所有 runtime 立即生效，无需任何同步步骤**
- 备份：`~/.agents/skills/last30days.bak-copy-3.21.0`（原始 3.21.0 拷贝）、`~/.claude/skills/last30days.bak-3.3.2`（旧版）。确认稳定后可删。
- 用户配置在 `~/.config/last30days/`，不在 skill 目录内。
- Claude Code marketplace 克隆 `~/.claude/plugins/marketplaces/last30days-skill` 已 git pull 到 v3.21.0（main），与本次开发无关，别动它。

## 3. 已完成：三个源模块（已真实 API 验证，2026-08-18）

都在 `skills/last30days/scripts/lib/` 下，均未提交（untracked）。**契约**：`search_<src>(topic, from_date, to_date, depth="default") -> Dict`（永不抛异常，失败返回空列表+`error` 键）+ `parse_<src>_response(response, query="") -> List[Dict]` + `DEPTH_CONFIG`。item 字段：`id, title, url, author, date(YYYY-MM-DD), engagement{}, snippet, relevance(0-1), why_relevant`。

### 3.1 `v2ex.py`
- 经 sov2ex：`GET https://www.sov2ex.com/api/search?q=<kw>&sort=created&size=<n>`（官方 API 无搜索）。
- `DEPTH_CONFIG = {"quick": 10, "default": 25, "deep": 50}`；overfetch ×2（上限 50）补偿客户端日期过滤。
- **quirks**：envelope 键是 `hits`（ES 风格，payload 在 `_source`）；engagement 只有 `{"replies": int}`（0 回复是正常的）；日期只有 naive datetime `created`（视作 UTC+8），`metadata.date_confidence="med"`、`metadata.node_id`；无鉴权无限流。

### 3.2 `juejin.py`
- `POST https://api.juejin.cn/search_api/v1/search`，**必须 `search_type=1`**（按时间排序；默认相关性排序几乎搜不到近 30 天内容）。`limit` 上限 20/页，DEPTH 用页数表达：`{"quick": {pages:1,count:10}, "default": {pages:2,count:25}, "deep": {pages:4,count:50}}`。
- **quirks**：envelope `{err_no, data, cursor, has_more}`，文章是 `result_type==2`；engagement 键为原生名 `digg_count/comment_count/view_count/collect_count`（normalizer 需映射 digg_count→likes）；日期 = `article_info.ctime`（unix 字符串，精确）；**软限流**：快速连调返回空 data 无报错，模块已内置浏览器 UA+Referer+页间 2s+重试，但流水线背靠背跑仍可能拿到空结果（empty 无 error ≠ 无结果）。

### 3.3 `bilibili.py`
- WBI 签名直连：`GET https://api.bilibili.com/x/web-interface/wbi/search/type?search_type=video&order=pubdate&keyword=...`，密钥从 `/x/web-interface/nav` 免登录获取，模块级缓存+锁。
- `DEPTH_CONFIG = {"quick": 10, "default": 25, "deep": 50}`（20/页，deep 翻 3 页）。
- **quirks**：envelope 键是 `result`（不是 `results`）；engagement 键 `views/likes/comments/danmaku/favorites/coins`（`comments` 实为回复数）；需浏览器 UA + `Referer: https://www.bilibili.com`；code `-412` = 风控限流（会写进 error）；日期 `pubdate` 精确；只保留 `type=="video"`。

三个模块的实测：`search_*('Claude Code', '2026-07-20', '2026-08-18')` 均返回 25 条真实数据。

## 4. 剩余工作（按序执行）

### 4.1 pipeline 接线（核心，全部在 `skills/last30days/scripts/`）

参照 xiaohongshu 的接法，但**去掉 opt-in 门**（默认启用，availability 谓词 = 恒 True，参考 hackernews 的接法）：

1. `lib/pipeline.py`：
   - import 三个模块（现有 import 区 ~line 73）
   - `MOCK_AVAILABLE_SOURCES`（~line 145-170）加 `"bilibili", "v2ex", "juejin"`
   - `SEARCH_ALIAS`（~line 92-99）可加 `"b站": "bilibili"`、`"xhs": "xiaohongshu"` 已有
   - `available_sources()`（~line 184-323）：三个源恒可用（无 key/无二进制依赖），仍受 `EXCLUDE_SOURCES` 约束
   - `_retrieve_stream_impl`（~line 4024 的大 if 分发，xiaohongshu 分支在 ~4603-4610）：加三个分支，调用 search_*+parse_*，返回 `(items, outcome_artifact)`
   - `_mock_stream_results`（~line 4622）：加 mock fixture
2. `lib/normalize.py` `normalizers` dict（~line 39-75）：v2ex/juejin 可仿 `_normalize_grounding` 或 hackernews 的 normalizer；bilibili 仿 `_normalize_shortform_video`（~line 436）。注意 engagement 键映射（见 3.2/3.3）。
3. `lib/render.py`：
   - `SOURCE_LABELS`（~line 215-235）加三个 label
   - `_FOOTER_SOURCES`（~line 2663-2739）加三个 tuple（emoji 自选不冲突即可，如 📺 bilibili / 🟢 v2ex / 📘 juejin；engagement 展示键与 3.x 一致）
   - raw-dump 顺序表（~line 1785-1795）加三个
   - **顺手修 bug**：`xiaohongshu` 缺失于 `_FOOTER_SOURCES`，补一个 tuple（engagement 用 likes/comments/collects，看 xiaohongshu_api.py 实际产出键）
4. 配套表项各加一行：`lib/signals.py` `SOURCE_QUALITY`（~line 12-32，中文源建议 0.7 同 xiaohongshu）、`lib/planner.py` `SOURCE_CAPABILITIES`（~line 136-160）、`lib/ui.py` `SOURCE_COMPLETION_META`（~line 136-153）、`lib/doctor.py` 源列表+record（~line 150-172, 840）、`lib/prescriptions.py`（~line 223-234）。

### 4.2 planner 中文子查询 + zhihu

- 读 `lib/planner.py` 的子查询生成逻辑（subquery 携带 `sources` 列表，fanout 在 `pipeline.py:2280-2320`）。
- 实现：当 topic 为拉丁字母（非 CJK，用 `lib/cjk.py` 的 `has_cjk()`）时，追加 1–2 条中文翻译子查询，`sources` 限定 `["bilibili", "v2ex", "juejin"]`。翻译本身由谁做：planner 若是规则代码则需要在 SKILL.md 里让模型在生成子查询时产出中文变体（planner 的子查询可能来自模型——先读代码确认生成方，**模型生成则改 SKILL.md 提示词，代码生成则在 planner.py 加钩子**）。
- zhihu：中文话题（或中文子查询）的 web 搜索追加 `site:zhihu.com`。同样先看 web 子查询是引擎发（brave backend）还是模型发（SKILL.md Step 2 的 WebSearch），两处可能都要加提示。

### 4.3 SKILL.md 文档点（`skills/last30days/SKILL.md`）

- front matter tags（~line 39-66）加 bilibili/v2ex/juejin（无新 env key——三源全部免鉴权）
- `ACTIVE_SOURCES_LIST` token→显示名映射（~line 757-766）
- Step 0.75 "Available sources"（~line 1407）
- Step 0.45 Class 5 非英文话题（~line 830-871）：中文话题说明新源自动覆盖；英文话题说明会追加中文子查询；zhihu site: 指引
- 统计 footer 模板示例（~line 1876-1880、~2256）
- Security & Permissions（~line 2263-2292）：披露新端点 `api.bilibili.com`、`www.sov2ex.com`、`api.juejin.cn`
- 若 planner 子查询是模型生成的，相应提示词段落也要改（见 4.2）

### 4.4 验收（Definition of Done）

```bash
cd D:/zita/kimicode/last30days-skill/skills/last30days/scripts
# mock 模式
python last30days.py "Claude Code" --emit=compact --search bilibili   # 及各源
# 真实端到端（英文话题：应看到 planner 产出中文子查询 + 三源有数据 + footer 三行）
python last30days.py "Claude Code" --emit=compact
# 真实端到端（中文话题）
python last30days.py "智谱 GLM 最近怎么样" --emit=compact
```

通过标准：footer 统计树出现 Bilibili/V2EX/掘金三行且数量>0；中文话题下 zhihu 内容经 web 源出现。若 repo 有 pytest 环境（`uv run pytest`，Python 3.12+），跑与改动物理相关的测试文件（normalize/render/pipeline 相关）。

## 5. 仓库红线（AGENTS.md 强制）

- 不 bump 任何版本号、不编辑 CHANGELOG.md、不写 changelog.d（自用分支）
- `lib/__init__.py` 必须保持裸包标记
- 所有 `log.source_log(...)` 必须 `tty_only=False`（CI 强制）
- 不提交真实密钥/cookie
- Windows 控制台中文显示乱码是 cp936 显示问题，数据本身是正确 UTF-8（验证用 `PYTHONIOENCODING=utf-8`）

## 6. 建议的执行方式

4.1+4.2 涉及大量共享文件（pipeline.py、normalize.py、render.py 等），**交给单个 agent 一次做完**，不要并行改这些文件。SKILL.md（4.3）与代码文件不相交，可并行。完成后按 4.4 验收。全部通过后 `git add -A && git commit`（用户未授权前不要 commit/push——先问）。
