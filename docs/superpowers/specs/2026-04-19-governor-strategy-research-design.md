# Governor 内部 Strategy Research 设计

## 1. 文档定位

本文档用于在现有 `玄枢 V1` 架构上补充一个关键慢路径能力：

**将“策略挖掘、参数搜索、历史回测、候选策略包输出”正式纳入 `Governor` 内部，而不是作为外部临时流程存在。**

本文档的目标不是讨论部署细节，而是明确：

- `Strategy Research` 为什么属于 `Governor`
- 它的职责边界是什么
- 它如何与 `Expert Layer`、`Decision Committee`、`Snapshot Publisher` 协同
- 它如何使用历史真实数据和 AI 辅助完成研究
- 它的输出如何进入正式治理流程并最终影响 `Trader`

本文档承接以下文档：

- [2026-04-18-xuanshu-v1-requirements-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-requirements-design.md)
- [2026-04-18-xuanshu-v1-architecture-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-architecture-design.md)
- [2026-04-18-xuanshu-v1-live-core-detailed-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-live-core-detailed-design.md)

## 2. 核心结论

本次设计结论如下：

- `Strategy Research` **不单独做成新服务**
- 它是 `Governor` 内部的一部分
- 它属于慢路径治理体系中的正式角色，并可视为 `Decision Committee` 的专家成员之一
- 它负责：
  - 策略挖掘
  - 参数搜索
  - 历史回测
  - 候选策略包生成
- 它不负责：
  - 直接下单
  - 直接改写 `Trader` 运行状态
  - 绕过 `Decision Committee` 直接发布策略
- `Trader` 永远只消费正式发布的 `StrategyConfigSnapshot`
- `Strategy Research` 的输出必须先经过 `Decision Committee` 审批，再由 `Snapshot Publisher` 转换并发布

## 3. 角色定位与职责边界

### 3.1 为什么它属于 Governor

策略研究、参数挖掘和历史回测，本质上都属于：

- 慢路径
- 离线或准离线计算
- 候选决策生成
- 治理输入构建

而不是热路径执行逻辑。

因此，它天然属于 `Governor`，而不属于：

- `Trader`：因为 Trader 只负责确定性执行
- `Notifier`：因为 Notifier 只负责运行观察和人工接管
- 外部临时流程：因为研究结果必须进入正式治理与审计链路

### 3.2 Governor 内部新结构

引入 `Strategy Research` 后，`Governor` 内部逻辑正式拆成四段：

- `Expert Layer`
- `Strategy Research`
- `Decision Committee`
- `Snapshot Publisher`

其中：

- `Expert Layer`：从当前状态与历史事实构建结构化治理意见
- `Strategy Research`：从历史真实数据出发，挖掘策略与参数，并给出候选策略包
- `Decision Committee`：综合专家意见与研究结果，决定哪些策略包可以进入正式候选
- `Snapshot Publisher`：把已批准结果转换成正式 `StrategyConfigSnapshot`

### 3.3 明确禁止的职责穿透

架构上必须明确禁止：

- `Strategy Research` 直接发布正式配置
- `Strategy Research` 直接改写 `Trader` 内部状态
- `Strategy Research` 直接向执行层下单
- AI 直接替代回测引擎输出可执行结果
- `Trader` 直接读取研究结果包

## 4. 输入数据边界

### 4.1 正式输入范围

`Strategy Research` 的正式输入不只包括行情数据，而是包括以下完整研究语境：

- 历史行情 / K 线
- 历史成交与交易相关市场数据
- 历史订单事实
- 历史持仓事实
- 历史风险事件
- 历史治理结果
- 当前及历史 snapshot
- 当前热状态摘要（作为研究上下文，而不是热路径依赖）

### 4.2 首期研究范围

首次研究范围限定为：

- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

但架构上保留扩展到更多 symbol 的能力。

### 4.3 数据使用原则

- 研究必须基于**历史真实数据**
- AI 可以参与研究分析与候选生成
- **真实历史回测、参数搜索执行、结果验证必须由系统自身完成**
- AI 不能替代回测引擎，也不能绕过回测结果直接决定参数

## 5. 触发机制

`Strategy Research` 正式支持三种触发方式：

### 5.1 周期性自动触发

按治理节拍或研究节拍定期触发，例如：

- 每日
- 每周
- 某固定交易时段后

适合做：

- 周期性参数复查
- 市场环境切换后的再评估
- 历史数据增量吸收后的滚动研究

### 5.2 人工命令触发

由人工明确请求触发，例如：

- 对某个 symbol 重做研究
- 对某类策略重新搜索参数
- 对某次异常行情后的表现做专项回测

适合做：

- 人工主导的研究任务
- 上线前专项验证
- 事后归因复查

### 5.3 事件触发

在特定治理事件或交易事件后触发，例如：

- 某策略连续失效
- 风险事件显著上升
- governor 连续冻结
- 市场 regime 长时间变化

适合做：

- 失效策略复盘
- 市场条件变化后的再研究
- 风险聚集后的参数重估

### 5.4 触发优先级原则

- 人工触发优先级最高
- 事件触发次之
- 周期触发最低

并且：

- 同一时间只允许受控数量的研究任务运行
- 不允许研究任务无限堆积影响治理主链

## 6. 研究对象与输出形式

### 6.1 输出不是参数建议，而是完整策略包

`Strategy Research` 的正式输出粒度定义为：

**完整策略包**

而不是只输出若干参数。

每个策略包至少包含：

- `strategy_package_id`
- `symbol_scope`
- `market_environment_scope`
- `strategy_family`
- `directionality`
- `entry_rules`
- `exit_rules`
- `position_sizing_rules`
- `risk_constraints`
- `parameter_set`
- `backtest_summary`
- `performance_summary`
- `failure_modes`
- `invalidating_conditions`
- `research_reason`
- `generated_at`

### 6.2 市场环境分桶

研究结果不是寻找“一套全局最优参数”，而是按市场环境分桶输出：

- `trend`
- `mean_reversion`
- `range`
- `stressed / high_volatility`
- `unknown / do_not_trade`

这意味着：

- 不同环境下可以对应不同候选策略包
- `Decision Committee` 可以决定某些环境只允许禁入或保护型策略

### 6.3 方向性要求

首发正式研究支持：

- **多空双向**

也就是说，研究结果可以包含：

- 多头 breakout
- 空头 breakout
- 多头回归
- 空头回归
- 区间上下沿双向反转

但是否正式启用，仍由 `Decision Committee` 决定。

## 7. 回测与参数搜索链路

### 7.1 回测链路目标

回测的目标不是“证明任何策略都能赚钱”，而是：

- 在历史真实数据上验证策略包是否成立
- 找到一组在不同市场环境下相对稳健的参数
- 暴露失效条件和风险特征
- 为 `Decision Committee` 提供正式决策材料

### 7.2 参数搜索的正式地位

参数搜索是正式研究能力的一部分，不是临时脚本行为。

它应支持：

- 搜索空间定义
- 参数组合试验
- 回测运行
- 结果排序
- 结果过滤
- 候选包生成

### 7.3 AI 与回测引擎的关系

AI 在这里负责：

- 帮助生成研究假设
- 提出策略变体
- 辅助整理候选参数空间
- 辅助解释研究结果

但 AI 不负责：

- 替代回测计算
- 直接决定最终参数
- 绕过回测验证输出可执行策略包

### 7.4 结果排序原则

回测结果不能只按收益排序。

`Strategy Research` 至少应综合以下维度排序：

- 收益表现
- 回撤
- 稳定性
- 交易次数与过拟合风险
- 市场环境适配度
- 失效条件是否清晰

## 8. 与 Decision Committee 的协同

### 8.1 正式交互方式

`Strategy Research` 输出的是：

- 候选策略包
- 研究摘要
- 回测统计
- 风险与失效说明

`Decision Committee` 必须基于这些内容进行审批。

### 8.2 审批要求

研究结果：

- **必须先经过 `Decision Committee` 审批**
- 不允许自动进入正式 snapshot

委员会至少要判断：

- 该策略包是否逻辑自洽
- 回测证据是否足够
- 风险是否在系统可接受范围内
- 是否适用于当前市场环境
- 是否应该进入正式候选、灰度启用或拒绝

### 8.3 审批结果类型

委员会对候选策略包至少可以给出：

- `approved`
- `approved_with_guardrails`
- `pending_review`
- `rejected`

## 9. 与 Snapshot Publisher / Trader 的关系

### 9.1 发布链路

正式发布链路为：

`Strategy Research -> Decision Committee -> Snapshot Publisher -> StrategyConfigSnapshot -> Trader`

### 9.2 Trader 的读取边界

`Trader` 永远只读取：

- 正式发布后的 `StrategyConfigSnapshot`

而不会直接读取：

- 研究包
- 回测结果
- 原始参数搜索结果

### 9.3 Snapshot 中保留的信息

`Snapshot Publisher` 应把被批准的策略包转译成当前执行层可消费的正式快照，包括但不限于：

- symbol whitelist
- strategy enable flags
- risk multiplier
- per-symbol max position
- market mode
- 审批状态
- 生效时间 / 过期时间
- 来源说明与版本号

必要时可在后续扩展 snapshot schema，以容纳更细的策略参数映射。

## 10. 首次部署与后续运行的关系

### 10.1 首次部署的定位

首次部署上线，不等于立刻拥有已完成回测定稿的最终交易策略。

首次部署的意义是：

- 让公司的正式交易底座成立
- 让 `Trader` / `Governor` / `Notifier` / 存储 / 恢复链路先稳定运行
- 为后续正式研究能力提供运行环境与历史事实积累基础

### 10.2 正式交易策略的形成过程

在首次部署后，完整交易策略的形成路径应为：

1. 运行底座上线
2. `Strategy Research` 使用历史真实数据做研究
3. 产出候选策略包
4. `Decision Committee` 审批
5. `Snapshot Publisher` 发布正式快照
6. `Trader` 依据正式快照执行

### 10.3 关键原则

因此，正式实盘策略不应来自：

- 临时人工拍脑袋定参数
- AI 未经回测直接给出的建议
- 研究结果直接越过委员会进入执行

而必须来自：

**真实历史数据研究 + 回测验证 + 委员会审批 + 正式快照发布**

## 11. 本阶段结论

本次设计的正式结论是：

- `Strategy Research` 是 `Governor` 内部正式能力
- 它负责策略挖掘、参数搜索、历史回测与完整策略包生成
- 它支持周期触发、人工触发和事件触发
- 它使用历史真实数据，并允许 `ChatGPT Pro` 参与研究分析
- 真实回测和参数验证仍由系统自身完成
- 它的输出必须先经过 `Decision Committee` 审批
- 只有被批准结果才能由 `Snapshot Publisher` 转换成正式 `StrategyConfigSnapshot`
- `Trader` 永远只消费正式 snapshot，而不直接读取研究结果

因此，`Governor` 的正式定位应从原来的：

**Expert Layer + Decision Committee + Snapshot Publisher**

扩展为：

**Expert Layer + Strategy Research + Decision Committee + Snapshot Publisher**

这使得 `Governor` 不只是“治理配置发布者”，也成为：

**真实量化机构中研究、评审、治理、发布一体化的慢路径决策中枢。**
