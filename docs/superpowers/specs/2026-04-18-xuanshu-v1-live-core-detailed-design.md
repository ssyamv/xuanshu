# 玄枢 V1 Live Core 详细设计

## 1. 文档定位

本文档用于承接：

- [2026-04-18-xuanshu-v1-requirements-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-requirements-design.md)
- [2026-04-18-xuanshu-v1-architecture-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-architecture-design.md)

并将其中的 `V1 live core` 部分细化为可直接进入工程实现的详细设计。

本文档只覆盖 `live core`，即：

- `Trader Service`
- `Governor Service`
- `Notifier Service`
- 热状态、审计状态、检查点状态
- 快路径执行闭环
- 慢路径治理闭环
- 单机部署前提下的服务协作方式
- 主要持久化对象与读写责任

本文档不覆盖：

- 回放 / Backtest
- MLflow / 晋升机制
- Qdrant 深度案例学习
- 多节点部署
- 完整数据库 schema
- 代码级类图与函数签名

本文档的目标是回答以下问题：

- 每个服务具体有哪些模块
- 模块之间怎么交互
- 哪些状态驻留在内存，哪些落 `Redis`，哪些落 `PostgreSQL`
- 哪些事件驱动快路径
- 哪些时机触发慢路径治理
- 故障和恢复时具体先做什么后做什么

## 2. Trader Service 详细设计

`Trader Service` 是 `live core` 的唯一热路径交易服务，也是状态、风控、执行和恢复的第一责任方。

### 2.1 进程职责

`Trader Service` 负责：

- 连接 `OKX` 公共/私有流
- 维护内存态市场与账户状态
- 消费当前有效治理快照
- 基于状态进行 `Regime` 路由和策略信号生成
- 做风控审核
- 生成执行动作并下发到交易接口
- 收敛订单回报和仓位回报
- 产出检查点
- 在重启或异常后触发恢复与对账

它不负责：

- AI 治理生成
- 通知编排
- 案例检索决策
- 长期审计分析

### 2.2 内部模块

建议在 `Trader Service` 内拆成 7 个模块：

- **Market Gateway**
  管理公共/私有 WebSocket 和必要的 REST 调用，输出统一事件，不做业务判断。

- **Event Dispatcher**
  接收 `Market Gateway` 的标准事件，按事件类型转发给状态、执行和恢复逻辑。

- **State Engine**
  维护运行时内存状态，包括：
  - 当前 BBO / books5 摘要
  - 最近成交偏置
  - 当前仓位
  - 当前挂单
  - 当前预算池
  - 当前运行模式
  - 当前 symbol 风险状态

- **Regime Router**
  读取 `State Engine` 当前快照，输出：
  - 当前 regime 标签
  - 当前优先策略集合
  - 是否应该进入禁入或暂停

- **Signal Factory**
  根据 regime 与策略篮子生成候选信号，输出统一候选信号对象，而不是直接下单请求。

- **Risk Kernel**
  对候选信号做最终审核，输出 `RiskDecision`。它必须是同步、确定性、无 AI 依赖的模块。

- **Execution Coordinator**
  把放行后的动作交给 `Execution Engine`，并负责：
  - 幂等 client order id
  - 下单/撤单/改单动作编排
  - 超时撤单
  - 回报关联
  - 与 `State Engine` 收敛状态

### 2.3 运行模式在 Trader 中的地位

`Trader Service` 内必须显式维护当前运行模式：

- `normal`
- `degraded`
- `reduce_only`
- `halted`

该模式的来源有两类：

- 快路径硬规则触发
- 治理快照指定或收紧

最终执行时取更保守者优先。

### 2.4 关键内存态对象

Trader 中建议显式维护以下运行时对象：

- `market_state_by_symbol`
- `position_state_by_symbol`
- `open_order_state_by_symbol`
- `budget_state_by_symbol`
- `latest_strategy_snapshot`
- `current_run_mode`
- `fault_flags`
- `last_public_stream_marker`
- `last_private_stream_marker`

这些对象的职责分工是：

- 内存：用于当前执行
- `Redis`：用于热状态映射和快速恢复辅助
- `PostgreSQL`：用于正式持久化与检查点依据

## 3. Governor Service 详细设计

`Governor Service` 是 `live core` 中唯一正式承载 AI 治理能力的服务。

### 3.1 进程职责

`Governor Service` 负责：

- 定期读取状态摘要和审计输入
- 生成结构化专家意见
- 汇总多专家意见形成治理裁决
- 发布新的 `StrategyConfigSnapshot`
- 在 AI 故障时保持治理状态可解释
- 把治理发布结果写入持久层并同步给热路径缓存

它不负责：

- 逐笔交易决策
- 风控硬闸
- 直接改写 Trader 内存状态
- 直接调用 `OKX` 下单

### 3.2 内部逻辑组件

虽然部署上是一个服务，详细设计上仍然分为三段：

- **Input Builder**
  从 `Redis / PostgreSQL` 读取治理需要的输入，整理成结构化治理上下文。输入包括：
  - 当前 `MarketStateSnapshot` 摘要
  - 当前运行模式
  - 最近风险事件摘要
  - 最近治理快照版本
  - 当前 symbol 交易状态摘要

- **Expert Layer**
  根据治理上下文生成结构化 `ExpertOpinion`。`live core` 阶段至少实现逻辑角色分层：
  - 市场结构专家
  - 风险专家
  - 事件过滤专家

- **Decision Committee**
  汇总多个 `ExpertOpinion` 形成统一裁决，至少回答：
  - 哪些 symbol 允许交易
  - 哪些策略启用/禁用
  - 当前风险倍率建议
  - 当前运行模式是否应收紧

- **Snapshot Publisher**
  把裁决结果转成正式 `StrategyConfigSnapshot`，写入：
  - `PostgreSQL` 版本记录
  - `Redis` 最新可生效快照缓存

### 3.3 触发方式

在 `live core` 中，Governor 采用双触发方式：

- **周期触发**
  固定周期运行，例如 `30s ~ 300s`

- **事件触发**
  发生以下情况时追加一次治理运行：
  - Trader 模式切换
  - 风险事件升级
  - 交易所连接异常恢复
  - 当前快照即将过期
  - `BTC / ETH` 出现异常波动标签

### 3.4 AI 故障策略

Governor 必须显式维护 AI 故障处理逻辑：

- 单次超时：本次治理放弃，不阻塞系统
- 连续超时：记录治理健康状态下降
- 当前快照仍有效：沿用最近有效快照
- 当前快照已过期且无法生成新快照：不得发布更激进配置，必要时只允许发布更保守状态或请求人工关注

### 3.5 核心持久化对象

Governor 在 `live core` 中正式读写以下对象：

- 读取：
  - 当前热状态摘要
  - 最近风险事件
  - 最近治理版本
  - 当前运行模式
- 写入：
  - `ExpertOpinion`
  - `StrategyConfigSnapshot`
  - 治理执行日志
  - AI 健康状态摘要

## 4. Notifier Service 与查询面详细设计

`Notifier Service` 是 `live core` 中的正式运行面和人工接管入口。

### 4.1 进程职责

它负责两类事：

- **主动推送**
  - 开仓/平仓通知
  - 止损和保护模式触发通知
  - AI 故障通知
  - 状态不一致和恢复失败通知
  - 运行模式切换通知

- **被动查询**
  - 当前模式
  - 当前仓位
  - 当前风险状态
  - 最近订单摘要
  - 当前系统健康状态

它不负责：

- 直接修改交易状态
- 直接决定运行模式
- 直接操作 `OKX`
- 持有热路径事实真相

### 4.2 输入来源

Notifier 不自己推导业务状态，只消费已有状态视图。

输入来源如下：

- `Redis`
  - 当前模式
  - 最新治理快照摘要
  - 当前热状态摘要
  - 当前异常标记

- `PostgreSQL`
  - 最近订单/成交摘要
  - 最近风险事件
  - 最近治理发布记录
  - 最近恢复记录

### 4.3 输出通道

`live core` 阶段，Notifier 统一通过 Telegram Bot 输出，分三类消息：

- `INFO`
  - 开仓
  - 平仓
  - 模式正常切换
  - 快照更新

- `WARN`
  - AI 超时增多
  - 网络抖动升高
  - 某 symbol 暂停
  - 进入 `degraded`

- `CRITICAL`
  - 进入 `reduce_only`
  - 进入 `halted`
  - 状态不一致
  - 恢复失败
  - 连续接口异常
  - 当前快照失效且治理不可用

### 4.4 查询命令面

详细设计里至少保留以下查询面：

- `/status`
- `/positions`
- `/orders`
- `/risk`
- `/mode`
- `/market`

### 4.5 异步与失败策略

Notifier 必须是异步的，并明确：

- 通知发送失败不得阻塞 Trader 或 Governor
- 失败消息需要记录可补发标记
- `CRITICAL` 级消息允许更高重试优先级
- 查询命令超时或失败，不影响交易主链路

## 5. 状态对象、主要持久化对象与读写责任

### 5.1 内存态对象

这些对象属于运行期内存态：

**Trader 内存态**
- `market_state_by_symbol`
- `position_state_by_symbol`
- `open_order_state_by_symbol`
- `budget_state_by_symbol`
- `current_run_mode`
- `fault_flags`
- `last_public_stream_marker`
- `last_private_stream_marker`

**Governor 内存态**
- `governor_health_state`
- `last_governor_run_at`
- `last_governor_result`
- `current_effective_snapshot_version`

**Notifier 内存态**
- `pending_notifications`
- `last_delivery_status`
- `command_request_context`

这些对象都：

- 为当前进程运行服务
- 可以重建
- 不是正式审计真相
- 不能独自承担恢复依据

### 5.2 Redis 中的热状态对象

`Redis` 中只保留当前运行最需要快速访问的对象：

- `latest_strategy_snapshot`
- `current_run_mode`
- `symbol_runtime_summary`
- `budget_pool_summary`
- `active_fault_flags`
- `governor_health_summary`

`Redis` 的设计目标是：

- 快路径快速读取
- Notifier 快速查询
- Governor 获取当前摘要
- 重启后提供热恢复辅助

### 5.3 PostgreSQL 中的主要持久化对象

在 `live core` 中，`PostgreSQL` 至少承载以下主要对象：

- `orders`
- `fills`
- `positions`
- `risk_events`
- `strategy_snapshots`
- `execution_checkpoints`
- `expert_opinions`
- `governor_runs`
- `notification_events`

对象职责：

- `orders / fills / positions`：交易事实
- `risk_events`：风控与保护触发事实
- `strategy_snapshots`：治理快照版本事实
- `execution_checkpoints`：恢复依据
- `expert_opinions / governor_runs`：治理过程审计
- `notification_events`：关键消息发送记录

### 5.4 Qdrant 中的主要对象

在 `live core` 中，`Qdrant` 只预留以下对象类型：

- `market_case`
- `risk_case`
- `governance_case`

它们只服务 Governor 后续治理增强，当前阶段不进入热路径依赖。

### 5.5 读写责任分工

- `Trader Service`
  - 读：`Redis` 最新快照、当前模式、热状态摘要
  - 写：`Redis` 热状态摘要、`PostgreSQL` 订单/成交/风险事件/检查点

- `Governor Service`
  - 读：`Redis` 当前热状态摘要、`PostgreSQL` 治理与风险历史、`Qdrant` 案例
  - 写：`PostgreSQL` 专家意见与治理快照、`Redis` 最新快照和治理健康摘要

- `Notifier Service`
  - 读：`Redis` 当前模式和摘要、`PostgreSQL` 最近事件
  - 写：`PostgreSQL` 通知记录
  - 不写业务运行状态

## 6. 关键时序设计

### 6.1 正常交易时序

正常交易时序定义为：

1. `Market Gateway` 接收 `OKX` 公共/私有事件
2. `Event Dispatcher` 按事件类型分发
3. `State Engine` 更新当前 symbol 的市场、订单、仓位、预算和模式状态
4. `Regime Router` 基于最新状态判断当前 regime
5. `Signal Factory` 生成候选信号
6. `Risk Kernel` 对候选信号做硬闸审核
7. 若放行，则 `Execution Coordinator` 调用 `Execution Engine` 生成下单动作
8. 下单结果和后续回报重新进入事件流
9. `State Engine` 收敛订单和仓位状态
10. 关键交易结果写 `PostgreSQL`，热状态摘要写 `Redis`
11. `Notifier Service` 发送必要通知

必须满足的约束：

- 风控审核在执行前是必经步骤
- 订单回报必须回流状态引擎

### 6.2 治理发布时序

治理发布时序定义为：

1. 到达治理周期，或触发治理事件
2. `Governor Service` 的 `Input Builder` 从 `Redis / PostgreSQL` 读取治理上下文
3. `Expert Layer` 基于输入生成结构化 `ExpertOpinion`
4. `Decision Committee` 汇总多个专家意见形成统一裁决
5. `Snapshot Publisher` 生成新的 `StrategyConfigSnapshot`
6. 新快照写入 `PostgreSQL` 版本记录
7. 最新可生效快照写入 `Redis`
8. `Trader Service` 在下一次读取配置时消费最新有效快照
9. `Notifier Service` 发送治理更新通知（如果需要）

关键约束：

- 治理发布是版本替换，不是直接改 Trader 内存对象
- Trader 只能通过读取最新有效快照生效

### 6.3 AI 故障治理时序

AI 故障治理时序定义为：

1. `Governor Service` 发起一次治理运行
2. AI 调用超时、失败或返回无效结构
3. 本次治理运行记为失败，写治理日志
4. 当前有效快照仍保留，不被替换
5. `Redis` 中的最新有效快照保持不变
6. `Notifier Service` 记录并视故障等级发送 `WARN` 或 `CRITICAL`
7. `Trader Service` 继续按最后有效快照运行
8. 若连续治理失败达到阈值，则系统进入更保守模式或要求人工关注

关键约束：

- AI 故障只影响更新能力，不直接破坏当前运行能力
- 治理层失败不能让快路径进入未定义状态

### 6.4 故障恢复时序

故障恢复时序定义为：

1. `Trader Service` 启动或检测到重连/异常恢复场景
2. 读取最近一次 `ExecutionCheckpoint`
3. 恢复本地已知模式、预算池和流标记
4. 重连公共/私有流
5. 调用 `OKX REST` 拉取当前订单和持仓事实
6. 与本地状态和检查点状态对账
7. 若一致，则恢复到可运行模式
8. 若不一致，则切换到 `reduce_only` 或 `halted`
9. 写恢复结果到 `PostgreSQL`
10. `Notifier Service` 发送恢复成功或失败通知

关键约束：

- 对账成功前，系统不能恢复新增风险
- 恢复流程的目标不是尽快重新下单，而是重新回到已知状态

## 7. 模块间接口约定与详细设计结论

### 7.1 模块间接口约定

在 `live core` 中，模块之间主要通过以下对象交互：

- `MarketEvent / OrderEvent / PositionEvent`
  用于 `Market Gateway -> Event Dispatcher -> State Engine / Execution Coordinator`

- `MarketStateSnapshot`
  用于 `State Engine -> Regime Router / Governor Input Builder`

- `CandidateSignal`
  用于 `Signal Factory -> Risk Kernel`

- `RiskDecision`
  用于 `Risk Kernel -> Execution Coordinator`

- `StrategyConfigSnapshot`
  用于 `Governor Service -> Redis / PostgreSQL -> Trader Service`

- `ExecutionCheckpoint`
  用于 `Trader Service <-> PostgreSQL`，支撑恢复与对账链

- `ExpertOpinion`
  用于 `Expert Layer -> Decision Committee`

这些对象必须满足：

- 对象是稳定契约，不是临时 dict 拼接
- 对象版本变化必须可追踪，尤其是治理快照和检查点

### 7.2 生效方式约定

必须写死以下生效方式：

- **市场事件**
  通过事件分发进入 Trader 内部模块，实时生效

- **治理快照**
  通过版本化写入 `PostgreSQL` 和缓存写入 `Redis` 生效，Trader 被动读取

- **运行模式**
  由快路径硬规则或治理快照共同决定，执行时取更保守者

- **恢复状态**
  通过加载检查点和对账结果生效，而不是通过重启时默认值生效

### 7.3 禁止的耦合方式

为防止实现阶段走偏，详细设计中明确禁止：

- 模块之间通过共享可变全局状态直接通信
- Trader 直接调用 Governor 内部逻辑拿即时 AI 结果
- Governor 直接写 Trader 内存对象
- Notifier 依赖 Trader 私有内存态作为唯一数据源
- 恢复逻辑跳过检查点直接按当前猜测状态继续运行
- `Redis` 中缓存对象被视为唯一真相来源

### 7.4 详细设计结论

到这一层，`V1 live core` 的详细设计已经形成一个清晰实现目标：

- `Trader Service` 作为唯一热路径交易核心
- `Governor Service` 作为正式 AI 治理核心
- `Notifier Service` 作为运行观察面和人工接管入口
- `Redis / PostgreSQL / Qdrant` 按强约束分工
- `StrategyConfigSnapshot` 作为治理和执行之间的唯一正式桥梁
- `ExecutionCheckpoint + Reconcile` 作为恢复链路核心
- 单机部署前提下仍保持逻辑边界清晰

因此，`玄枢 V1 live core` 的详细设计定位是：

**一个可以直接进入实现阶段的单机 `live core` 工程模型，围绕状态对象、治理快照、风控决策、事件闭环和恢复链路组织，而不是一组原则性描述。**
