### 分析1

**P0（先止血，1天内可做）**
1. 多骨架渲染替代单骨架  
在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 的 `render_user_fortune_reply_v2` 增加 `STYLE_BLUEPRINTS`（至少 6 套），按 `question_type + session_id + query_hash` 选骨架，禁止连续两轮同骨架。
2. 建议语句去固定表单  
把 `_default_fortune_advice` 从“每类3条固定句”改成“每类12-20条建议池 + 去重抽样（按 session 最近N轮避重）”。
3. 命理信号改为多源拼装  
把 `_signal_for_topic` 从单字段映射改成“主信号+次信号”组合，来源包括 `career/love/wealth/simple_desc/strength`，避免长期固定一句。

**P1（修根因，2-3天）**
1. 增强意图识别，减少误入时间兜底  
在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 的 `is_bazi_fortune_query` 与 `detect_question_type` 增加动作型命理短句模式（如“先做什么/怎么安排更顺/该抓哪件事”），保证走 fortune pipeline 而不是 time fallback。
2. 时序兜底改“保结构”而非“替换整段”  
`validate_time_consistency` 不要整段替换为统一提示，改为仅修正时间窗行，保留其余分析内容。
3. 证据驱动建议  
在 [mytools.py](/Users/yayauu/PycharmProjects/fortune-telling/mytools.py) 把 `fortune_signals` 扩展字段（例如 `risk_hint/opportunity_hint`），渲染时按证据选建议，降低“模板建议”占比。

**P2（质量治理，持续）**
1. 加“结构重复率”指标  
在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 指标体系新增 `blueprint_repeat_rate`、`advice_repeat_rate`。
2. 更新门禁  
在 [scripts/quality_gate.py](/Users/yayauu/PycharmProjects/fortune-telling/scripts/quality_gate.py) 增加阈值：`advice_repeat_rate <= 0.25`、`blueprint_repeat_rate <= 0.30`。
3. 回归集扩容  
在 [docs/13-运势分析V2问题测试结果-2026-02-27.md](/Users/yayauu/PycharmProjects/fortune-telling/docs/13-运势分析V2问题测试结果-2026-02-27.md) 同类问题加入“同义改写+连续追问”组，专测抗模板化。

**预期效果（按你当前问题）**
1. “结论/时间窗口/命理信号/依据/建议”不再固定同序同句。
2. “先稳住节奏…”这类建议不再高频重复。
3. “我这周先做什么”类问题会更稳定走命理链路，不会大量出现时间兜底模板。

-----
### 分析2
**目标**
在不牺牲 `直答率/时窗一致性/不回显资料` 的前提下，把命理回复从“固定模板”改为“结构可控、表达多样、内容更贴题”。

**解决方案**
1. 重构渲染层为“语义槽位 + 多蓝图”  
文件：[server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py:1768)  
做法：把当前固定顺序渲染改成蓝图库（每类问题 4-6 个蓝图），蓝图只约束槽位，不固定句式。  
槽位建议：`直答结论`、`命理证据`、`时间窗口/触发日`、`行动建议`、`风险边界`。  
效果：结构仍可验收，但文本不再同构。

2. 丰富上游可用字段，减少“固定句池兜底”  
文件：[mytools.py](/Users/yayauu/PycharmProjects/fortune-telling/mytools.py:147)  
做法：在 `BaziToolOutput` 增加 `risk_points`、`opportunity_points`、`time_hints`、`evidence_lines`；`advice` 从“固定表”改为“字段驱动生成 + 兜底”。  
效果：同一 topic 下也能因证据不同而输出不同内容。

3. 增加“会话去重器”，控制连续轮次重复  
文件：[server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py:547)  
做法：保存最近 N 轮 `blueprint_id + 归一化首句`，若重复则切换蓝图并重采样措辞。  
效果：避免 `S1/S3` 高相似复现。

4. 调整时间校验策略，避免整段模板替换  
文件：[server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py:1049)  
做法：`validate_time_consistency` 发现越界时只修正“日期片段”，保留正文；仅严重错配时才回退整段。  
效果：保住时序正确性，同时减少“我先把时间对齐”泛滥。

5. 更新质量门禁，从“字段命中”升级为“质量 + 多样性”  
文件：[scripts/fortune_regression.py](/Users/yayauu/PycharmProjects/fortune-telling/scripts/fortune_regression.py:55)、[scripts/quality_gate.py](/Users/yayauu/PycharmProjects/fortune-telling/scripts/quality_gate.py:12)  
做法：保留现有硬指标，新增：  
- 同类问题首句去重率 >= 0.7  
- 连续 3 轮最大相似度 <= 0.82  
- 有效证据行（非固定句）>= 1  
效果：防止“为了过回归而模板化”。

**实施顺序（建议）**
1. 先改渲染蓝图与去重器（最快见效）。  
2. 再扩展 `BaziToolOutput` 字段并改 advice 生成。  
3. 最后改时间校验细粒度修正与回归门禁。  

**验收标准**
- docs/13 全量用例 HTTP 通过率不下降。  
- 决策直答命中率维持 >= 95%。  
- 趋势/口语时窗命中率维持 >= 90%。  
- 同类问题模板重复率降到 <= 20%，连续追问最高相似度 <= 0.82。  
----
### 分析3
下面给你一个可落地的解决方案，按“低风险快速见效 -> 中期稳定提升”分层。

**目标**
1. 保留命理可控性（不乱说、可回溯）。
2. 显著降低“同骨架复读”。
3. 不破坏现有时间一致性与质量指标链路。

**P0（先做，1天内）**
1. 改造渲染器为“多模板簇”而不是单模板。  
文件：[server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py)  
把 `render_user_fortune_reply_v2` 从固定 `结论/窗口/信号/依据/建议` 改为按 `question_type` 选择不同模板簇（例如 decision/trend/action/colloquial 各 3-5 套）。
2. 扩展命理信号选择逻辑，避免 `daily/study` 总落同一信号。  
文件：[server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py)  
重写 `_signal_for_topic`，从 `fortune_signals` + `strength` + `wuxing_scores` 组合生成，不再只取单字段。
3. 让建议从“固定句库”变“证据驱动生成”，固定句库只做兜底。  
文件：[mytools.py](/Users/yayauu/PycharmProjects/fortune-telling/mytools.py), [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py)  
保留 `_build_advice/_default_fortune_advice` 作为 fallback，但主路径优先根据 payload + query 动态生成 2-3 条。
4. 调整 `sanitize_output`，减少对正常差异表达的抹平。  
文件：[server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py)  
保留安全清洗，放宽对开场句与语气词的统一化处理。

**P1（再做，2-3天）**
1. 加“会话级去重重写”机制。  
文件：[server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py)  
在 fortune 分支返回前，计算与最近1-3轮相似度（3-gram/Jaccard），超阈值只重写“结论+建议”，不动“依据+时间窗口”。
2. 增加“问题类型强约束渲染”。  
decision 必须首句二选一；trend/colloquial 必须给窗口；action 必须给“第一步”。  
落点：`detect_question_type` 后进入专用 renderer。
3. 把 `build_style_instruction` 的非重复策略用于命理分支。  
目前命理分支早返回，绕过通用风格链，需在 fortune 渲染阶段接入。

**P2（治理，持续）**
1. 将“模板重复率/唯一输出率”加入门禁。  
文件：[scripts/quality_gate.py](/Users/yayauu/PycharmProjects/fortune-telling/scripts/quality_gate.py)  
新增指标：`unique_output_rate`、`section_repeat_rate`。
2. 升级回归脚本，显式检测“格式死板”。  
文件：[scripts/fortune_regression.py](/Users/yayauu/PycharmProjects/fortune-telling/scripts/fortune_regression.py)  
同类问题连续提问时，要求首句与建议段差异度达阈值。
3. 用 feature flag 灰度发布，失败可秒回滚。  
继续用现有 flags，在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 扩展 `render_v3` 或 `anti_repeat_v1`。

**验收标准（建议）**
1. docs/13 的 30 条回放中，`unique_outputs >= 24`。  
2. 相同骨架整段重复次数从当前高频（多次）降到 `<= 2`。  
3. decision 首句直答率 `>= 95%`。  
4. trend/colloquial 时间窗口命中率 `>= 95%`。  
5. 不降低 `temporal_consistency_hit_rate` 与 `observability_coverage`。
。