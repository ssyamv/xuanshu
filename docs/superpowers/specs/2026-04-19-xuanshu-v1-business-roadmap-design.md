# 玄枢 V1 业务功能路线文档

## 1. 文档定位

本文档用于把现有需求设计、架构设计和详细设计中已经正式纳入 `V1 / 1.0` 范围的业务功能，整理成一份统一的开发路线文档。

本文档只覆盖业务功能，不覆盖以下保障项：

- 部署与环境编排
- 监控与告警基础设施
- 日志体系
- 自动化测试体系本身
- 运维手册
- 数据库 schema 细化

本文档的用途是：

- 明确 `1.0` 到底要做完哪些业务功能
- 给每项功能标注当前状态
- 定义每项功能的业务验收标准
- 给出后续逐项开发的推荐顺序

本文档承接以下文档：

- [2026-04-18-xuanshu-v1-requirements-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-requirements-design.md)
- [2026-04-18-xuanshu-v1-architecture-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-architecture-design.md)
- [2026-04-18-xuanshu-v1-live-core-detailed-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-live-core-detailed-design.md)
- [2026-04-19-trader-live-execution-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-19-trader-live-execution-design.md)

## 2. 1.0 范围边界

`1.0` 的正式范围，以现有设计文档里已经明确纳入 `V1 live core` 的业务能力为准。

纳入 `1.0` 的业务主线只有三条：

- `Trader Service` 快路径交易闭环
- `Governor Service` 慢路径治理闭环
- `Notifier Service` 运行可见性与人工接管闭环

不纳入 `1.0` 的内容包括：

- 回放 / Backtest
- MLflow / 晋升机制
- Qdrant 深度案例学习
- 多节点部署
- 数据库 schema 细化
- 纯基础设施类建设项

因此，本文档中的“完成 `1.0`”不等于“平台所有想法都做完”，而是等于：

- `live core` 的三条正式业务闭环全部成立
- 系统可以在设计约束下完成真实业务运行
- AI 治理、快路径执行、通知接管之间的业务关系闭环成立

## 3. 状态定义

为了后续逐项推进，本文档统一使用三种状态，并在每个功能标题前显式标记：

- `[已完成]`
  代码与测试已经基本兑现该功能，且功能边界已经清晰落地。

- `[部分完成]`
  已有契约、模块、适配器或部分运行逻辑，但业务闭环还没真正打通，或者存在明显缺口。

- `[未开始]`
  设计里已明确要求该功能，但当前仓库里还没有形成有效实现。

其中：

- `[已完成]` 视为 `1.0` 已完成项
- `[部分完成]` 和 `[未开始]` 都视为 `1.0` 未完成项

## 4. 当前完成情况总览

### 4.1 已完成项

以下功能当前可判定为 `[已完成]`：

- Trader：`T1` 到 `T12`
- Governor：`G1` 到 `G9`

### 4.2 未完成项

以下功能当前都属于 `1.0` 未完成项：

- Notifier：`N1` 到 `N5`

## 5. Trader Service 业务功能清单

### [已完成] T1. 实时市场接入

- 功能说明：
  接入 `OKX` 公共流和私有流，统一获取行情、订单、持仓、账户回报，并转成标准事件。
- 当前状态：`已完成`
- 当前依据：
  - `OKX public/private websocket` 已支持 `tickers / trades / orders / positions / account` 的标准事件解码
  - Trader runtime 已接入公私流事件消费入口，能把流事件持续送入主闭环
  - 异常消息会统一转成 `runtime_fault`
- 依赖：
  - `T2` 事件分发
  - `T3` 状态更新
- 验收标准：
  - Trader 进程启动后，能够持续消费配置 symbol 的公共流和私有流
  - 公共流至少覆盖 `tickers / trades`
  - 私有流至少覆盖 `orders / positions / account`
  - 异常消息会被转成统一 fault 事件，而不是静默丢失

### [已完成] T2. 统一事件分发

- 功能说明：
  将市场、订单、持仓、账户、故障等标准事件显式路由到状态、执行和恢复逻辑。
- 当前状态：`已完成`
- 当前依据：
  - `contracts/events.py` 已定义 Trader 标准事件
  - `dispatch_event()` 已覆盖 `orderbook_top / market_trade / order_update / position_update / account_snapshot / runtime_fault`
  - 不支持的事件会显式抛错
- 依赖：
  - `T1` 实时市场接入
  - `T3` 状态更新
- 验收标准：
  - 所有 Trader 标准事件都能进入统一分发入口
  - 事件分发覆盖至少：`orderbook_top`、`market_trade`、`order_update`、`position_update`、`account_snapshot`、`runtime_fault`
  - 不支持的事件会显式失败，不允许静默吞掉

### [已完成] T3. 运行时状态引擎

- 功能说明：
  在内存中维护当前市场、持仓、挂单、模式、流标记、故障标记等 Trader 事实状态。
- 当前状态：`已完成`
- 当前依据：
  - `StateEngine` 已维护 quotes、recent trades、orders、positions、账户摘要、fault flags、run mode、stream markers
  - 已支持输出 symbol 级 runtime summary 和 budget/account summary
  - Redis 热状态发布已消费这些摘要
- 依赖：
  - `T1`
  - `T2`
- 验收标准：
  - 每个 symbol 都能维护最新 BBO、最近成交偏置、当前仓位、当前挂单摘要
  - 进程能维护 `current_run_mode`
  - 能维护 `last_public_stream_marker` 和 `last_private_stream_marker`
  - 能输出 symbol 级运行摘要，供治理和通知消费

### [已完成] T4. Regime 路由

- 功能说明：
  根据当前 `MarketStateSnapshot` 识别市场状态，并确定优先策略方向或保护行为。
- 当前状态：`已完成`
- 当前依据：
  - `classify_regime()` 已稳定区分 `trend / mean_reversion / range / unknown`
  - 异常市场状态会回落到保护性输出，而不是继续沿用常规交易路线
- 依赖：
  - `T3`
- 验收标准：
  - 至少能区分 `trend / mean_reversion / range / unknown`
  - 路由结果能够稳定驱动后续信号生成
  - 异常状态下能够落入保护型输出，而不是继续沿用常规交易路线

### [已完成] T5. 信号工厂

- 功能说明：
  根据当前 regime 和策略篮子生成候选交易信号，而不是直接下单。
- 当前状态：`已完成`
- 当前依据：
  - `build_candidate_signals()` 已产出趋势、均值回归和保护型候选信号
  - Trader runtime 已在市场事件到达后接通信号生成与后续风控/执行链路
- 依赖：
  - `T4`
  - `T6`
- 验收标准：
  - 每次状态评估都能输出零个或多个 `CandidateSignal`
  - 趋势环境、均值回归环境、禁入环境三类输出行为明确不同
  - 信号只表达候选动作，不直接产生交易副作用

### [已完成] T6. 风控硬闸

- 功能说明：
  对候选信号做最终确定性审核，给出是否允许开仓、允许平仓、当前风控模式和头寸上限。
- 当前状态：`已完成`
- 当前依据：
  - `RiskKernel` 已对审批状态、生效窗口、过期、symbol 白名单、strategy 开关和 `reduce_only / halted` 做同步确定性审核
  - 风控输出保持结构化 `RiskDecision`，平仓路径默认保留
- 依赖：
  - `T5`
  - `G4`
- 验收标准：
  - 以下情况必须阻止新增风险：
    - snapshot 未审批
    - snapshot 未生效
    - snapshot 已过期
    - symbol 不在白名单
    - strategy 被禁用
    - 模式为 `reduce_only` 或 `halted`
  - 平仓路径默认允许
  - 风控输出必须是同步、确定性的结构化 `RiskDecision`

### [已完成] T7. 执行引擎

- 功能说明：
  将放行后的交易动作转成确定性的下单意图和 `OKX` payload。
- 当前状态：`已完成`
- 当前依据：
  - `build_client_order_id()` 和 `build_market_order_payload()` 已稳定产生确定性市价开仓 payload
  - 执行层仍保持纯函数边界，不引入网络副作用
- 依赖：
  - `T6`
  - `T8`
- 验收标准：
  - 能为市价开仓生成稳定 payload
  - client order id 具备幂等性
  - 不允许在执行层直接引入网络副作用

### [已完成] T8. 执行协调器

- 功能说明：
  接收允许执行的意图，发起 REST 下单，追踪 inflight intent，并做回报关联。
- 当前状态：`已完成`
- 当前依据：
  - `ExecutionCoordinator` 已处理幂等 market open 提交、inflight/completed intent 跟踪和参数冲突显式报错
  - 交易所确认结果已能回流 Trader runtime，并进入事实持久化
- 依赖：
  - `T7`
  - `T9`
- 验收标准：
  - 同一 `client_order_id` 重试不会重复下单
  - 参数冲突时显式报错
  - 能记录 inflight 和 completed intent
  - 能将交易所确认结果回传给 Trader 运行时

### [已完成] T9. 回报收敛与状态回写

- 功能说明：
  将订单确认、成交、持仓变化重新收敛回状态引擎，并产出热状态与持久化事实。
- 当前状态：`已完成`
- 当前依据：
  - 订单更新和持仓更新已回写 `StateEngine`
  - 关键状态变化会刷新 Redis symbol summary / run mode / budget summary / fault flags
  - 订单、成交、持仓、风险事件和 checkpoint 已写入 PostgreSQL runtime store
- 依赖：
  - `T2`
  - `T3`
  - `T8`
- 验收标准：
  - 订单更新能回写 open order 状态
  - 持仓更新能回写当前持仓状态
  - 关键状态变化会触发 Redis hot summary 更新
  - 关键交易事实会写入 PostgreSQL

### [已完成] T10. 启动恢复与对账

- 功能说明：
  在启动或故障恢复时，读取检查点，拉取交易所真实状态，对账后决定能否恢复新增风险。
- 当前状态：`已完成`
- 当前依据：
  - Trader 启动时会先读取最近 checkpoint
  - `RecoverySupervisor` 已接入启动流程，并拉取 open orders / positions 做对账
  - 对账不一致时会收紧模式并阻止新增风险
- 依赖：
  - `T1`
  - `T3`
  - `T8`
- 验收标准：
  - 启动时会先读取最近 checkpoint
  - 会拉取当前 open orders / positions
  - checkpoint 与交易所状态不一致时，系统进入 `reduce_only` 或 `halted`
  - 对账成功前，不允许新增风险

### [已完成] T11. 运行模式切换

- 功能说明：
  在快路径规则和治理快照共同作用下维护 `normal / degraded / reduce_only / halted`。
- 当前状态：`已完成`
- 当前依据：
  - Trader 明确维护 `current_run_mode`
  - 启动 gating、恢复结果和 runtime fault 都已接入模式切换
  - 模式变化遵循“更保守优先”，并同步发布到热状态
- 依赖：
  - `T6`
  - `T10`
  - `G5`
- 验收标准：
  - Trader 明确维护当前运行模式
  - 模式变化遵循“更保守优先”
  - 模式变化会被热状态发布并能被治理/通知消费

### [已完成] T12. Trader 主闭环

- 功能说明：
  把行情接入、状态更新、路由、信号、风控、执行、回报、模式维护真正串成一个持续运行的业务闭环。
- 当前状态：`已完成`
- 当前依据：
  - Trader 已把行情接入、状态更新、路由、信号、风控、执行、回报、模式维护串成可运行闭环
  - 快路径执行不依赖 AI 同步返回
  - 运行异常会收紧模式并留下风险/恢复事实
- 依赖：
  - `T1` 到 `T11`
- 验收标准：
  - Trader 启动后，在有效配置和安全状态下可持续完成一轮完整业务闭环
  - 快路径不依赖 AI 同步返回
  - 出现异常时会收紧而不是失控

## 6. Governor Service 业务功能清单

### [已完成] G1. 治理输入构建

- 功能说明：
  从当前热状态、最近风险事件、最新快照和历史治理记录中构建治理上下文。
- 当前状态：`已完成`
- 当前依据：
  - `GovernorService.build_state_summary()` 已存在
  - 输入结构已覆盖治理闭环所需核心上下文，可直接供专家层、委员会和 AI runner 消费
- 依赖：
  - `T3`
  - `T11`
- 验收标准：
  - 输入至少包含：当前 run mode、最新 snapshot version、active fault flags、symbol summaries、recent risk events、recent governor runs
  - 输入可以直接送入专家层和 AI 生成器

### [已完成] G2. 专家层

- 功能说明：
  对治理上下文进行结构化分析，至少产出市场结构、风险、事件过滤三类专家意见。
- 当前状态：`已完成`
- 当前依据：
  - 已有 `build_expert_opinions()`
  - 已能产出结构化 `ExpertOpinion`
- 依赖：
  - `G1`
- 验收标准：
  - 至少输出三类专家意见：
    - market_structure
    - risk
    - event_filter
  - 每条意见包含 decision、confidence、supporting_facts、risk_flags、ttl

### [已完成] G3. 决策委员会

- 功能说明：
  汇总专家意见，形成统一治理裁决，包括建议模式底线、阻断标记和人工复核要求。
- 当前状态：`已完成`
- 当前依据：
  - 已有 `build_committee_summary()`
- 依赖：
  - `G2`
- 验收标准：
  - 能从专家意见中汇总 blocking flags
  - 能给出 `recommended_mode_floor`
  - 能给出是否需要人工复核

### [已完成] G4. AI 治理生成

- 功能说明：
  调用正式 AI 治理执行器，输出新的 `StrategyConfigSnapshot`。
- 当前状态：`已完成`
- 当前依据：
  - `ConfiguredGovernorAgentRunner.run()` 已接通真实治理执行路径
  - Governor 已能生成结构化 `StrategyConfigSnapshot` 并进入后续校验/发布流程
- 依赖：
  - `G1`
  - `G2`
  - `G3`
- 验收标准：
  - Governor 能通过真实配置的 AI runner 生成结构化 snapshot
  - 输出结果经过 schema 校验后才能进入后续流程
  - AI 不直接改写 Trader 状态，只能产出治理快照

### [已完成] G5. 治理护栏与模式收紧

- 功能说明：
  对 AI 产出的候选 snapshot 进行二次护栏约束，禁止不合理放宽风险。
- 当前状态：`已完成`
- 当前依据：
  - 已有 `apply_guardrails()`
  - 可根据 faults / risk events / current mode 做模式和 multiplier 收紧
- 依赖：
  - `G4`
- 验收标准：
  - 有 active fault flags 时，模式不会比 `degraded` 更宽松
  - 有恢复失败时，approval state 进入保守状态并停止新增风险放宽
  - observed symbols 不会被错误排除

### [已完成] G6. 快照发布

- 功能说明：
  将治理结果写入 `PostgreSQL` 与 `Redis`，作为最新可生效快照发布给 Trader。
- 当前状态：`已完成`
- 当前依据：
  - Governor runtime 已能发布 snapshot 到 store，并写入历史记录
  - 最新可读快照和 governor run 历史已能稳定供 Trader/Notifier 消费
- 依赖：
  - `G5`
- 验收标准：
  - 新 snapshot 发布后，Redis 中存在最新可读版本
  - PostgreSQL 中存在版本记录和 governor run 记录
  - Trader 可以在后续读取中消费该版本

### [已完成] G7. 治理周期调度

- 功能说明：
  支持周期触发和事件触发两类治理执行入口。
- 当前状态：`已完成`
- 当前依据：
  - 当前 runtime loop 已有 schedule / event trigger reason 判断框架
  - 周期触发与事件触发入口均已接通到正式治理执行链路
- 依赖：
  - `G1`
  - `G6`
- 验收标准：
  - 周期触发可以按 interval 正常运行
  - 模式切换、风险升级、快照即将过期等事件可以触发追加治理
  - 非 schedule 触发时可立即进入下一轮治理

### [已完成] G8. AI 故障冻结策略

- 功能说明：
  在 AI 调用失败、超时或输出无效时，冻结最近有效快照并保持治理状态可解释。
- 当前状态：`已完成`
- 当前依据：
  - 已有 `freeze_on_failure()` 和 health summary 框架
  - AI 失败、超时或输出无效时，已能冻结最近有效 snapshot 并更新治理健康摘要
- 依赖：
  - `G4`
  - `G6`
- 验收标准：
  - 单次失败不会阻塞系统
  - 最近有效 snapshot 保持可用
  - 连续失败计数会进入治理健康摘要
  - 快照过期且无法生成新快照时，不允许发布更激进配置

### [已完成] G9. 治理主闭环

- 功能说明：
  将输入构建、专家分析、委员会裁决、AI 生成、护栏、发布、健康更新串成完整慢路径治理闭环。
- 当前状态：`已完成`
- 当前依据：
  - 闭环骨架已在 `GovernorRuntime` 中存在
  - 输入构建、专家分析、委员会裁决、AI 生成、护栏、发布、健康更新已形成正式慢路径治理闭环
- 依赖：
  - `G1` 到 `G8`
- 验收标准：
  - Governor 能周期性地产生或冻结一个正式有效的 snapshot
  - Trader 能消费到新 snapshot
  - 治理故障不会把系统推进未定义状态

## 7. Notifier Service 业务功能清单

### [部分完成] N1. 主动推送

- 功能说明：
  向 Telegram 推送关键业务事件，包括模式变化、治理更新、风险事件、恢复失败等。
- 当前状态：`部分完成`
- 当前依据：
  - Telegram 发送路径已存在
  - Notifier 已能投递 runtime started、pending/proactive notifications
- 依赖：
  - `T11`
  - `G6`
  - `N4`
- 验收标准：
  - 至少能推送以下业务事件：
    - 模式变化
    - snapshot 发布
    - 风险事件
    - 恢复失败
  - `CRITICAL` 消息具备更高重试优先级

### [部分完成] N2. 查询命令面

- 功能说明：
  通过 Telegram 命令查询当前模式、市场摘要、仓位、订单、风险状态。
- 当前状态：`部分完成`
- 当前依据：
  - 已支持 `/status /positions /orders /risk /mode /market /takeover`
- 依赖：
  - `T3`
  - `T11`
  - `G6`
- 验收标准：
  - 支持设计中要求的全部查询命令
  - 命令响应来源于现有热状态与历史事实，而不是 Notifier 自己推导
  - 查询失败不影响交易主链路

### [部分完成] N3. 人工接管

- 功能说明：
  允许人工通过命令发起受控 takeover，请求系统进入更保守的运行模式。
- 当前状态：`部分完成`
- 当前依据：
  - `/takeover` 已存在
  - 已能提升 mode 并写入 fault flag / risk event
- 依赖：
  - `T11`
- 验收标准：
  - 人工请求只允许将系统推向更保守状态
  - 请求会留下结构化审计事实
  - Trader / Governor 后续都能读取到该变化

### [部分完成] N4. 通知分级与补发

- 功能说明：
  对通知做 `INFO / WARN / CRITICAL` 分级，失败后记录可补发状态，关键消息允许重试。
- 当前状态：`部分完成`
- 当前依据：
  - `deliver_text()` 已实现 severity 差异化重试
  - 已有 pending / proactive 两类刷新逻辑
- 依赖：
  - `N1`
- 验收标准：
  - 发送成功会记录 sent 事件
  - 发送失败会记录 failed 事件
  - `CRITICAL` 失败后带 retry 标记
  - flush 过程不会阻塞核心业务进程

### [部分完成] N5. 运行可见性闭环

- 功能说明：
  让人工监督者能够持续看到当前模式、风险状态、最近治理结果和关键异常。
- 当前状态：`部分完成`
- 当前依据：
  - 当前 `status`、`risk`、`market` 已有雏形
  - 但效果取决于上游 Trader/Governor 是否把摘要真正发布完整
- 依赖：
  - `T3`
  - `T11`
  - `G9`
- 验收标准：
  - `/status` 能看到当前 mode、snapshot、faults、budget、governor health
  - `/risk` 能看到最近风险事件
  - `/market` 能看到 symbol runtime summaries
  - 运行可见性不依赖人工登录数据库或手工查日志

## 8. 统一开发顺序

为了让 `1.0` 能最快形成真实可运行业务闭环，推荐按以下顺序开发。

### 第一阶段：补 Trader 快路径最短闭环

优先项：

- `T6` 风控硬闸修正
- `T1` 实时市场接入
- `T2` 统一事件分发
- `T3` 运行时状态引擎补齐
- `T12` Trader 主闭环

原因：

- Trader 是唯一热路径执行者
- 不先让快路径闭环成立，Governor 和 Notifier 都只能围绕半成品运行

阶段验收：

- Trader 能持续消费 `OKX` 公私有事件
- 能从行情走到信号、风控、执行、回报回写
- 能在不安全状态下主动收紧模式

### 第二阶段：补恢复与执行反馈闭环

优先项：

- `T8` 执行协调器补齐
- `T9` 回报收敛与状态回写
- `T10` 启动恢复与对账
- `T11` 运行模式切换

原因：

- 这是从“能跑”到“敢跑”的分界线
- 没有恢复和回报收敛，真实交易无法受控

阶段验收：

- 启动先恢复再交易
- checkpoint 与交易所不一致时能自动收紧
- 订单、持仓、风险事件能持续回写事实层

### 第三阶段：完成 Governor 真正治理闭环

优先项：

- `G4` AI 治理生成
- `G5` 治理护栏
- `G6` 快照发布
- `G8` AI 故障冻结策略
- `G9` 治理主闭环

原因：

- Trader 主闭环先成立后，治理的价值才能真正进入 live 系统
- Governor 完成后，快路径与慢路径的正式业务关系才算成立

阶段验收：

- Governor 能生成真实 snapshot 或冻结旧 snapshot
- Trader 能消费快照变化
- 治理失败不会使系统进入未定义状态

### 第四阶段：补 Notifier 运行面闭环

优先项：

- `N1` 主动推送
- `N2` 查询命令面
- `N3` 人工接管
- `N4` 通知分级与补发
- `N5` 运行可见性闭环

原因：

- Notifier 依赖上游业务事实完整发布
- 让它放在最后收口，能避免先做大量空响应或假数据

阶段验收：

- 人工能通过 Telegram 看清当前运行状态
- 人工能发起受控 takeover
- CRITICAL 异常不会静默丢失

## 9. 当前版本总体判断

从当前仓库状态看，`1.0` 业务功能整体处于以下阶段：

- `Trader Service`：`已完成`
- `Governor Service`：`已完成`
- `Notifier Service`：`部分完成`

更具体地说：

- 契约、基础模块、适配器和运行骨架已经基本具备
- Trader 的完整 live 主闭环已经接通
- Governor 的慢路径治理闭环已经接通，能够生成、护栏化、发布或冻结正式 snapshot
- Notifier 已有较完整的交互面，但其最终价值仍取决于上游业务闭环补齐

因此，当前项目还不能视为接近 `1.0` 完成状态，而是处在：

**Trader 快路径闭环与 Governor 慢路径治理闭环已完成，当前只剩 Notifier 运行面闭环待补齐。**

## 10. 逐项开发的执行原则

后续应严格按本文档逐项推进，每一项都遵循以下原则：

- 先补闭环，再补扩展
- 先让 Trader 可控运行，再让 Governor 稳定治理闭环，最后让 Notifier 完成运行面收口
- 不把文档外的新想法提前塞进 `1.0`
- 每开发完一项，就更新本文件中的状态和备注

后续每一轮开发，推荐都显式标注：

- 本轮完成了哪些功能项
- 哪些功能项状态发生变化
- 哪些后续项因此解锁
