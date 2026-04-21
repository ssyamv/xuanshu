# 玄枢 V1 Live Core 详细设计

## 1. 文档定位

本文档用于承接：

- [2026-04-18-xuanshu-v1-requirements-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-requirements-design.md)
- [2026-04-18-xuanshu-v1-architecture-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-architecture-design.md)

并将其中的 `V1 live core` 部分细化为可直接进入工程实现的详细设计。

本文档只覆盖 `live core`，即：

- `Trader Service`
- `Notifier Service`
- 热状态、审计状态、检查点状态
- 快路径执行闭环
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
- 故障和恢复时具体先做什么后做什么

> 2026-04-21 更新：当前运行架构已经从早期 `Trader + Governor + Notifier` 收敛为 `Trader + Notifier + Redis + PostgreSQL`。Governor/Qdrant/AI 研究链路已从运行时移除；保留的核心路径是固定策略快照、Trader 热路径执行、Notifier 人工控制、Redis 热状态和 PostgreSQL 审计事实。

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

当前固定策略运行形态下，运行模式还有一条人工控制路径：

- `notifier` 通过 Telegram 命令写入 Redis 运行态。
- `trader` 在事件分发和逐标的评估前读取 Redis 控制项。
- 更保守的模式变更立即生效，例如 `normal -> halted`。
- 更宽松的人工解除只在快照已批准、检查点允许新风险、且无故障标记时生效。
- 生效后的模式、故障标记、预算摘要和 symbol 摘要由 `trader` 重新发布回 Redis。

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
- `manual_release_target`
- `manual_strategy_total_amount_override`

这些对象的职责分工是：

- 内存：用于当前执行
- `Redis`：用于热状态映射和快速恢复辅助
- `PostgreSQL`：用于正式持久化与检查点依据

## 3. Governor Service 历史设计

`Governor Service` 是早期 `live core` 中承载 AI 治理能力的服务。当前运行时已不再部署该服务，相关设计仅作为历史背景保留，不是生产操作入口。

当前生产运行规则：

- 不通过 Governor 发布新策略。
- 不通过 Qdrant 或 AI research/provider 链路影响交易热路径。
- 当前有效策略来自经过审阅的固定 `StrategyConfigSnapshot` 文件。
- `trader` 启动时优先加载 `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH`，避免 Redis 中旧快照覆盖固定策略。
- 策略切换必须先生成/审阅固定快照，再通过部署配置切换文件路径。

### 3.1 进程职责

历史设计中，`Governor Service` 负责：

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

历史设计中，Governor 采用双触发方式：

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

历史设计中，Governor 必须显式维护 AI 故障处理逻辑：

- 单次超时：本次治理放弃，不阻塞系统
- 连续超时：记录治理健康状态下降
- 当前快照仍有效：沿用最近有效快照
- 当前快照已过期且无法生成新快照：不得发布更激进配置，必要时只允许发布更保守状态或请求人工关注

### 3.5 核心持久化对象

历史设计中，Governor 读写以下对象：

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

- `/help`
- `/status`
- `/positions`
- `/position`
- `/orders`
- `/risk`
- `/mode`
- `/market`
- `/pause [reason]`
- `/start [reason]`
- `/resume [reason]`
- `/takeover <mode> [reason]`
- `/release <mode> [reason]`
- `/capital <amount> [reason]`

其中：

- `/pause` 直接请求 `halted` 并写入 `manual_pause` 故障标记。
- `/start` / `/resume` 清理人工暂停/接管标记，并写入 `normal` 释放目标。
- `/release` 写入指定释放目标，由 `trader` 判定是否可以安全放宽。
- `/capital` 写入 `strategy_total_amount` 和 `manual_strategy_total_amount_override=true`，由 `trader` 同步到起始 NAV 与风控 NAV。
- `/positions` 优先读取 Redis 当前运行态持仓摘要；没有运行态摘要时才回退到最近持仓事实。
- `/status` 优先展示模式、快照、故障、账户权益、策略总金额、运行控制、策略逻辑和运行摘要，不再暴露内部预算字段。

### 4.5 异步与失败策略

Notifier 必须是异步的，并明确：

- 通知发送失败不得阻塞 Trader
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
- `manual_strategy_total_amount_override`

**Governor 内存态（历史设计，当前运行时不部署）**
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
- `manual_release_target`

`Redis` 的设计目标是：

- 快路径快速读取
- Notifier 快速查询
- 重启后提供热恢复辅助
- 在 `notifier` 与 `trader` 之间承载人工控制请求

### 5.3 PostgreSQL 中的主要持久化对象

在 `live core` 中，`PostgreSQL` 至少承载以下主要对象：

- `orders`
- `fills`
- `positions`
- `risk_events`
- `strategy_snapshots`
- `execution_checkpoints`
- `notification_events`

对象职责：

- `orders / fills / positions`：交易事实
- `risk_events`：风控、保护触发和人工控制审计事实
- `strategy_snapshots`：固定策略快照版本事实
- `execution_checkpoints`：恢复依据
- `notification_events`：关键消息发送记录

### 5.4 Qdrant 中的主要对象

`Qdrant` 是历史治理增强设计，当前运行时不部署，也不进入生产热路径。历史上预留过以下对象类型：

- `market_case`
- `risk_case`
- `governance_case`

它们只服务历史 Governor 治理增强设计，当前阶段不进入热路径依赖。

### 5.5 读写责任分工

- `Trader Service`
  - 读：`Redis` 最新快照、当前模式、热状态摘要
  - 写：`Redis` 热状态摘要、`PostgreSQL` 订单/成交/风险事件/检查点

- `Governor Service`
  - 当前运行时不部署；不得作为生产策略发布或人工操作入口。

- `Notifier Service`
  - 读：`Redis` 当前模式和摘要、`PostgreSQL` 最近事件
  - 写：`Redis` 人工运行控制、`PostgreSQL` 通知记录和人工控制审计事件
  - 不直接调用 OKX，也不直接改写 Trader 内存

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

### 6.2 固定策略发布时序

当前运行时的策略发布时序定义为：

1. 离线生成或手工审阅固定 `StrategyConfigSnapshot` 文件。
2. 将固定快照放到生产服务器受控路径。
3. 设置 `XUANSHU_FIXED_STRATEGY_SNAPSHOT_PATH` 指向该文件。
4. 以 `XUANSHU_DEFAULT_RUN_MODE=halted` 启动或重启运行栈。
5. `Trader Service` 启动时优先加载固定快照。
6. `Trader Service` 将有效快照和运行态摘要写入 Redis。
7. `Notifier Service` 通过 `/status` 展示快照版本、策略逻辑、运行模式和持仓摘要。
8. 操作员检查日志、检查点和运行摘要后，再通过 `/start` 或 `/release` 请求恢复交易。

关键约束：

- 策略发布是文件版本切换，不是 Telegram 命令直接改策略。
- 启动默认必须保持保护模式。
- 固定快照优先级高于 Redis 中可能残留的旧快照。

### 6.3 人工控制时序

人工控制时序定义为：

1. 操作员在 Telegram 发送 `/pause`、`/start`、`/release` 或 `/capital`。
2. `Notifier Service` 校验命令并写入 Redis 控制项。
3. `Notifier Service` 写入对应 `manual_*` 风控审计事件。
4. `Trader Service` 在事件分发或逐 symbol 评估前读取 Redis 控制项。
5. 若是更保守模式，立即收紧。
6. 若是释放到更宽松模式，必须先通过快照、检查点和故障标记检查。
7. 若释放成功，`Trader Service` 清除释放目标并写入 `manual_release_applied`。
8. 若释放不满足条件，保持当前更保守模式，等待后续状态变干净。

关键约束：

- Notifier 不直接调用 OKX，不直接改 Trader 内存。
- 人工命令可以请求运行态变化，但最终是否放宽由 Trader 判定。
- 人工命令审计事件不作为主动风险告警反复推送。

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
  用于 `State Engine -> Regime Router`；历史设计中也可用于 `Governor Input Builder`

- `CandidateSignal`
  用于 `Signal Factory -> Risk Kernel`

- `RiskDecision`
  用于 `Risk Kernel -> Execution Coordinator`

- `StrategyConfigSnapshot`
  用于 `fixed snapshot file / Redis / PostgreSQL -> Trader Service`

- `ExecutionCheckpoint`
  用于 `Trader Service <-> PostgreSQL`，支撑恢复与对账链

这些对象必须满足：

- 对象是稳定契约，不是临时 dict 拼接
- 对象版本变化必须可追踪，尤其是治理快照和检查点

### 7.2 生效方式约定

必须写死以下生效方式：

- **市场事件**
  通过事件分发进入 Trader 内部模块，实时生效

- **策略快照**
  通过固定文件和缓存写入 `Redis` 生效，Trader 启动时优先读取固定文件

- **运行模式**
  由快路径硬规则、固定快照和人工 Redis 控制共同决定，执行时取更保守者；放宽必须由 Trader 做安全判定

- **恢复状态**
  通过加载检查点和对账结果生效，而不是通过重启时默认值生效

### 7.3 禁止的耦合方式

为防止实现阶段走偏，详细设计中明确禁止：

- 模块之间通过共享可变全局状态直接通信
- Trader 直接调用 Governor 内部逻辑拿即时 AI 结果
- Governor 直接写 Trader 内存对象
- Notifier 依赖 Trader 私有内存态作为唯一数据源
- Notifier 直接调用 OKX 或直接改 Trader 内存
- 恢复逻辑跳过检查点直接按当前猜测状态继续运行
- `Redis` 中缓存对象被视为唯一真相来源

### 7.4 详细设计结论

到这一层，`V1 live core` 的详细设计已经形成一个清晰实现目标：

- `Trader Service` 作为唯一热路径交易核心
- `Notifier Service` 作为运行观察面和人工控制入口
- `Redis / PostgreSQL` 按强约束分工
- 固定 `StrategyConfigSnapshot` 作为策略发布和执行之间的唯一正式桥梁
- `ExecutionCheckpoint + Reconcile` 作为恢复链路核心
- 单机部署前提下仍保持逻辑边界清晰

因此，`玄枢 V1 live core` 的详细设计定位是：

**一个可以直接进入实现阶段的单机 `live core` 工程模型，围绕状态对象、固定策略快照、人工控制、风控决策、事件闭环和恢复链路组织，而不是一组原则性描述。**
