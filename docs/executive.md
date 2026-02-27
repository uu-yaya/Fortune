### 1. T1 路由补洞（优先级 P0）  
改动点：在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 调整 `is_bazi_fortune_query` 与 `detect_question_type` 规则，补齐短决策/动作问法命中；把 `action` 判定优先级前置，减少被 `trend/colloquial` 吞掉；新增路由观测 reason code（如 `fortune_short_decision_hit`）。  
测试点：构造 30 条短问法矩阵（如“开源还是守财”“这周先做什么”“先扩收入还是先控支出”），核验 `route_path=fortune_pipeline` 命中率；复跑 docs/13，确认 `decision` 直答率与窗口命中率不下降。  
回滚点：新增开关 `INTENT_ROUTING_V3`（默认关）；线上异常时只需关闭该开关并重启服务，即回到现有路由规则。

### 2. T2 多蓝图渲染替代单骨架（优先级 P0）  
改动点：在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 将 `render_user_fortune_reply_v2` 改为“蓝图库 + 槽位渲染”；按 `question_type + session_id + query_hash` 选蓝图；加入“最近1轮不可同蓝图”约束。  
测试点：复跑 docs/13 与文档19同类问题集，统计 `unique_outputs`、最大相似度、首句重复率；同时回归 `decision/trend/colloquial` 必达字段是否仍满足。  
回滚点：新增开关 `RENDER_V3`（默认关）；关闭后直接走当前 `render_user_fortune_reply_v2` 旧逻辑。

### 3. T3 信号与建议改为证据驱动（优先级 P1）  
改动点：在 [mytools.py](/Users/yayauu/PycharmProjects/fortune-telling/mytools.py) 扩展 `BaziToolOutput`（如 `risk_points/opportunity_points/time_hints/evidence_lines`）；在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 重写 `_signal_for_topic` 为组合生成，并让建议优先使用证据字段，固定建议表仅兜底。  
测试点：做新旧 payload 兼容测试（字段缺失不报错）；抽检 20 条回复“有效证据行”命中；确认无资料回显、无空建议。  
回滚点：新增开关 `EVIDENCE_ADVICE_V1`（默认关）；关闭后恢复旧 `_signal_for_topic + _default_fortune_advice` 路径。

### 4. T4 时间一致性改局部修补（优先级 P1）  
改动点：在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 的 `validate_time_consistency` 引入“局部修正函数”，只替换错配的日期/窗口行；仅在严重错配（年份越界、多处冲突）时才走整段 fallback。  
测试点：构造“日期对、星期错”“窗口越界”“年份漂移”样本，确认修正后正文保留；复跑 docs/13，确保时窗命中率和时间正确率不降。  
回滚点：新增开关 `TIME_PATCH_V1`（默认关）；关闭后恢复当前整段 fallback 行为。

### 5. T5 指标与门禁升级（优先级 P1）  
改动点：在 [server.py](/Users/yayauu/PycharmProjects/fortune-telling/server.py) 的质量埋点新增 `blueprint_repeat_rate/advice_repeat_rate/unique_output_rate/max_pair_similarity`；更新 [scripts/quality_gate.py](/Users/yayauu/PycharmProjects/fortune-telling/scripts/quality_gate.py) 阈值；更新 [scripts/fortune_regression.py](/Users/yayauu/PycharmProjects/fortune-telling/scripts/fortune_regression.py) 的抗模板断言。  
测试点：先跑一组“已知模板化样本”验证门禁能失败，再跑改造后样本验证门禁通过；确保旧核心指标（直答率、时窗命中、观测覆盖）仍达标。  
回滚点：保留 `--legacy-metrics`/`QUALITY_GATE_LEGACY` 兼容模式；门禁误杀时可切回旧阈值与旧指标集合。