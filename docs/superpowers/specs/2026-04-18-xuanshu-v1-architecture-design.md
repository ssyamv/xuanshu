# 玄枢 V1 架构设计

## 1. 文档定位

本文档用于承接 [2026-04-18-xuanshu-v1-requirements-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-18-xuanshu-v1-requirements-design.md)，将已确认的需求边界转化为可进入详细设计的 `V1` 架构方案。

本文档的设计深度定位为：

- 完整运行架构
- 覆盖逻辑分层
- 覆盖核心服务划分
- 覆盖数据流、状态流、配置流、恢复流
- 覆盖存储分工
- 覆盖部署拓扑与演进方向

本文档不直接展开以下内容：

- 时序图级别的详细交互
- 类、函数、接口字段级设计
- 数据库 schema 细节
- 任务拆分和开发排期

本文档面向你自己和未来协作者，目标是保证后续详细设计和实现阶段都基于同一套清晰边界推进。

## 2. 架构目标与设计原则

`玄枢 V1` 的架构目标，不是只把模块拼成一个能运行的系统，而是构建一个在 `live` 场景下仍能保持控制权、状态一致性和恢复能力的 `Trading Harness`。

它必须同时服务两条主线：

- 业务主线：支撑 `OKX` 永续合约短线 `live` 交易，形成持续正收益能力的基础
- 系统主线：保证交易行为受状态契约、风控硬约束、治理快照和恢复链路控制

为此，`V1` 的架构设计原则明确如下：

- **快慢路径分离**：快路径负责实时交易闭环；慢路径负责低频治理、配置与审计。快路径不得等待慢路径同步返回。
- **状态契约优先**：系统依赖结构化状态对象、配置快照和检查点运行，而不是依赖 prompt、自由文本或人工记忆维持一致性。
- **执行确定性优先**：所有交易动作必须由确定性逻辑、风控硬约束和执行状态机驱动，AI 不进入逐笔热路径。
- **恢复优先于继续交易**：重启、断线、异常后，先恢复和对账，再决定是否恢复新增风险。
- **服务边界清晰**：交易执行、AI 治理、通知、存储必须有明确职责，避免后续模块互相渗透。
- **部署从简但职责不混乱**：`V1` 部署可以简单，但逻辑分层不能因为单机部署而被写糊。

## 3. 系统分层与核心服务架构

`玄枢 V1` 采用两层表达：

- 逻辑架构：说明系统内部有哪些职责单元
- 运行架构：说明这些职责单元如何落到服务和部署上

### 3.1 逻辑分层

正式定义四层：

- **接入与事件层**
  负责接入 `OKX` 公共/私有流，统一生成标准化市场与账户事件。

- **执行与控制热路径层**
  负责状态更新、策略路由、信号生成、风控审核、执行状态机、订单回报处理。

- **治理与配置慢路径层**
  负责 `Expert Layer`、`Decision Committee`、`Snapshot Publisher`、审计分析与 AI 治理。

- **基础设施与存储层**
  负责 `Redis`、`PostgreSQL`、`Qdrant`、通知、监控、配置和运行时基础能力。

### 3.2 核心服务

在运行架构上，`V1` 先落成以下服务：

- **Trader Service**
  负责快路径核心闭环：
  - 行情接入后的事件消费
  - `State Engine`
  - `Regime Router`
  - `Signal Factory`
  - `Risk Kernel`
  - `Execution Engine`
  - `Checkpoint / Reconcile` 入口

- **Governor Service**
  对外是一个治理服务，对内逻辑拆成：
  - `Expert Layer`
  - `Decision Committee`
  - `Snapshot Publisher`

  它负责：
  - 读取状态摘要和审计输入
  - 调用 AI 治理能力
  - 生成新的 `StrategyConfigSnapshot`
  - 发布新的治理配置给快路径

- **Notifier Service**
  负责：
  - Telegram 推送
  - 查询命令
  - 告警补发
  - 消息节流和分级

- **Storage Components**
  包括：
  - `Redis`
  - `PostgreSQL`
  - `Qdrant`

### 3.3 服务关系

正式关系如下：

- `Trader Service` 是唯一热路径交易执行者
- `Governor Service` 不能直接下单，只能发布配置
- `Notifier Service` 不能阻塞 `Trader` 或 `Governor`
- `Storage` 只提供状态、审计、检索和恢复支持，不参与业务决策

## 4. 核心模块职责与边界

### 4.1 Trader Service 内部模块

`Trader Service` 内部应明确拆成以下模块：

- **Market Bus Adapter**
  接入 `OKX` 公共/私有流，将行情、成交、订单、持仓、回报统一转成标准事件。

- **State Engine**
  维护当前市场状态、仓位状态、订单状态、预算状态和运行模式相关状态。

- **Regime Router**
  根据 `MarketStateSnapshot` 判断当前更接近趋势、回归还是异常状态，并决定优先启用哪类策略或是否进入禁入逻辑。

- **Signal Factory**
  根据当前路由结果和策略篮子生成候选信号，输出候选动作，而不是最终交易动作。

- **Risk Kernel**
  作为交易前最后的硬约束层，负责：
  - 是否允许开仓
  - 是否必须只减仓
  - 是否进入保护模式
  - 当前风险上限

- **Execution Engine**
  负责下单、撤单、改单、幂等键、超时撤单、回报收敛和执行状态机。

- **Checkpoint / Reconcile Module**
  负责生成检查点、恢复状态、重连后对账，并在一致性未恢复时阻止新增风险。

### 4.2 Governor Service 内部逻辑组件

对外它是一个服务，对内逻辑必须拆成三段：

- **Expert Layer**
  对市场状态、系统状态、风险状态和审计输入进行结构化分析，形成专家意见。

- **Decision Committee**
  整合多个专家意见，形成统一裁决，只负责是否发布新的治理配置。

- **Snapshot Publisher**
  把裁决结果落成正式的 `StrategyConfigSnapshot`，写入存储并对 `Trader` 暴露为最新可生效配置。

### 4.3 明确禁止的职责穿透

架构上必须明确禁止：

- `State Engine` 直接调用 AI
- `Signal Factory` 绕过 `Risk Kernel`
- `Execution Engine` 自己决定策略路由
- `Governor Service` 直接调用交易接口下单
- `Notifier Service` 直接决定交易模式
- `Qdrant` 进入热路径直接查询
- AI 结果直接改写执行状态，只能通过快照发布链路生效

## 5. 关键数据流、状态流与配置流

`玄枢 V1` 依赖四条正式主链运行：

- 市场数据流
- 交易执行流
- 治理配置流
- 异常恢复流

### 5.1 市场数据流

正式路径：

`OKX Public/Private Streams -> Market Bus Adapter -> State Engine -> Regime Router / Signal Factory / Execution Reconciliation`

关键要求：

- 公共行情和私有回报必须都进入统一事件模型
- `State Engine` 是实时状态汇聚点
- 策略与风控读状态快照，而不是直接读原始流
- 订单回报不仅影响执行结果，也反向修正状态和预算池

### 5.2 交易执行流

正式路径：

`State Snapshot -> Regime Router -> Candidate Signal -> Risk Kernel -> Execution Engine -> Order/Fill Updates -> State Engine`

关键要求：

- `Signal Factory` 只产生候选动作
- `Risk Kernel` 是开仓和模式切换前的最后硬闸
- `Execution Engine` 只做被授权动作的执行
- 回报更新必须回写 `State Engine` 形成闭环

### 5.3 治理配置流

正式路径：

`State Summary / Audit Input -> Expert Layer -> Decision Committee -> Snapshot Publisher -> StrategyConfigSnapshot -> Trader Cache`

关键要求：

- AI 治理层不直接碰热路径状态
- 治理结果必须落成结构化快照
- 快路径只读取最近一版有效快照
- 快照必须具备生效时间、有效期和版本号
- 快照过期或治理失败时，不生成新的风险放宽动作

### 5.4 状态流

状态流分层如下：

- **热状态流**
  当前市场状态、仓位、订单、预算、当前模式，由 `Trader` 内部维护，并同步到 `Redis`

- **结构化持久状态流**
  订单、成交、风险事件、快照版本、检查点、审计结论，写入 `PostgreSQL`

- **案例检索状态流**
  历史案例、治理经验、相似情境，写入 `Qdrant`，仅供慢路径治理使用

### 5.5 异常恢复流

正式路径：

`Load ExecutionCheckpoint -> Reconnect Streams -> Fetch Exchange Truth -> Reconcile Local State -> Decide Mode -> Resume or Halt`

关键要求：

- 恢复优先于继续交易
- 对账前不得新增风险
- 对账成功后才能回到 `normal/degraded`
- 对账失败时必须进入 `reduce_only` 或 `halted`

## 6. 存储架构与强约束分工

### 6.1 Redis：热状态与当前运行事实

`Redis` 只承担热状态职责，主要包括：

- 当前最新 `StrategyConfigSnapshot`
- 当前运行模式
- 当前预算池
- 当前 symbol 级状态摘要
- 当前短期异常标记
- 快路径需要快速读取的临时状态

它的角色是为运行提供低延迟状态访问，而不是充当审计真相库。

### 6.2 PostgreSQL：结构化事实与恢复依据

`PostgreSQL` 是正式事实库，应存储：

- 订单
- 成交
- 仓位快照
- 风控事件
- `StrategyConfigSnapshot` 版本
- `ExecutionCheckpoint`
- 审计记录
- 治理发布记录

它的角色是系统的持久化真相来源、审计依据和恢复依据。

### 6.3 Qdrant：案例检索，仅限慢路径

`Qdrant` 只负责：

- 相似市场情境检索
- 历史失败案例检索
- 风控否决案例召回
- 治理层经验辅助输入

它的角色是为治理层提供相似经验，不为快路径提供逐笔决策依赖。

### 6.4 强约束分工

架构文档必须明确：

- `Trader Service` 热路径读取 `Redis`，可以写 `PostgreSQL`，不得依赖 `Qdrant`
- `Governor Service` 可以读 `Redis` 状态摘要、读 `PostgreSQL` 审计记录、读 `Qdrant` 案例，但不得直接改写 `Trader` 内部运行状态
- `Notifier Service` 可以读 `Redis` 和 `PostgreSQL` 的必要视图，但不能写业务状态
- `ExecutionCheckpoint` 的正式持久化归 `PostgreSQL`
- 最新可生效快照可以缓存在 `Redis`，但版本真相必须落在 `PostgreSQL`

## 7. 故障、降级与恢复架构

故障处理是 `V1` 的正式架构主链，而不是附属说明。

### 7.1 故障分类

架构上建议把故障分成三类：

- **可恢复故障**
  短时网络抖动、临时接口错误、单次 AI 超时、短时通知失败

- **高风险故障**
  连续接口异常、状态不同步、连续重连失败、回报异常累积、治理层失效持续存在

- **致命故障**
  恢复失败、对账失败、核心状态失真严重、运行模式无法确认

### 7.2 模式切换架构

系统正式支持以下运行模式：

- `normal`
- `degraded`
- `reduce_only`
- `halted`

模式切换原则：

- 快路径可以基于硬规则直接切换保护模式
- 慢路径可以发布更保守的治理快照
- 严重故障时以快路径硬保护优先，不等待 AI 决策

### 7.3 故障处理链路

正式链路：

`Fault Detected -> Fault Classification -> Mode Decision -> Notification -> Recovery Attempt -> Reconcile -> Resume or Halt`

归属如下：

- 故障发现主要发生在 `Trader`
- 故障分类和保护决策优先由确定性规则完成
- 通知由 `Notifier` 异步发送
- 恢复与对账由 `Checkpoint / Reconcile` 逻辑负责
- 恢复到何种模式，必须基于对账结果和当前治理配置

### 7.4 AI 故障的特殊处理

由于 AI 治理层是 `V1` 的正式能力，因此 AI 故障必须单独定义：

- AI 不可用、超时或结果过期时，系统不得中断快路径
- 系统继续冻结在最近一次有效治理配置上运行
- 不允许基于失效 AI 结果发布新的风险放宽动作
- 如果 AI 故障持续时间超过架构设定阈值，可触发更保守模式或要求人工关注

### 7.5 恢复架构

恢复流程必须是正式架构主链：

1. 读取最近一次 `ExecutionCheckpoint`
2. 恢复本地已知模式、预算池和状态快照
3. 重连公共流和私有流
4. 拉取交易所当前订单与仓位事实
5. 与本地状态对账
6. 对账成功则恢复交易
7. 对账失败则进入 `reduce_only` 或 `halted`

最重要的约束：

- 恢复优先于继续交易
- 对账成功前禁止新增风险
- 本地缓存从来不能覆盖交易所真相

## 8. 部署拓扑与演进路线

`V1` 部署设计的目标是稳定、清晰、可维护、可回滚，而不是追求平台化复杂度。

### 8.1 V1 推荐部署拓扑

`V1` 推荐采用：

**单主交易节点 + 单机部署所有业务服务 + 明确恢复链路**

你已经确认，`V1` 会把所有服务放在一台服务器上，因此正式推荐拓扑写为：

- 一台主交易服务器承载：
  - `Trader Service`
  - `Governor Service`
  - `Notifier Service`
  - `Redis`
  - `PostgreSQL`
  - `Qdrant`
  - 必要的监控与日志采集组件

这是一套全服务单机部署方案，适用于：

- `V1` 验证阶段
- 模拟盘与小资金 `live`
- 单人或小团队维护

其优势在于：

- 拓扑简单
- 故障面清楚
- 回滚路径直接
- 易于做镜像、配置和快照版本管理

其边界在于：

- 单机故障风险较高
- 不适合一开始就做大规模扩展

### 8.2 服务部署原则

即便所有服务在一台服务器上，架构上也必须写死以下原则：

- `Trader Service` 是唯一主交易服务，不做多活交易
- `Governor Service` 与 `Trader` 同机部署，但逻辑边界独立
- `Notifier Service` 故障不得影响 `Trader` 和 `Governor`
- `Redis` 必须服务于热路径低延迟访问
- `PostgreSQL` 负责事实持久化与恢复依据
- `Qdrant` 仅服务慢路径治理
- 不允许多个地域同时活跃下单

### 8.3 上线与回滚架构

部署架构必须支持：

- 镜像版本化
- 配置版本化
- `StrategyConfigSnapshot` 版本化
- 一键回滚到上一稳定版本
- 回滚时必要进入 `reduce_only` 或 `halted`

### 8.4 演进路线

推荐的演进路线如下：

- **V1.5**
  - PostgreSQL / Redis 托管化或独立化
  - 审计与回放节点独立
  - 慢路径治理与审计能力增强
  - 监控和告警体系完善

- **V2**
  - 更复杂的治理流程
  - 更丰富的策略和 symbol 扩展
  - 更明确的多节点服务拆分
  - 更强的评测、晋升和案例学习能力

明确非目标：

- `V1` 不直接上 Kubernetes
- `V1` 不直接做分布式多主交易
- `V1` 的演进前提是先验证单主交易架构在真实运行中的稳定性

## 9. 本阶段结论

`玄枢 V1` 的架构结论可以概括为：

- 它采用快慢路径分离架构
- 它以 `Trader Service` 作为唯一热路径交易核心
- 它以 `Governor Service` 作为统一部署、逻辑拆分的治理核心
- 它通过 `StrategyConfigSnapshot` 连接治理层与执行层
- 它通过 `Redis / PostgreSQL / Qdrant` 的强约束分工维持热路径、事实层和案例层边界
- 它把故障、降级和恢复视为正式主链
- 它在 `V1` 阶段采用全服务单机部署，以换取实现和运维简洁性

因此，`玄枢 V1` 的正式架构定位是：

**一个面向 `OKX` 永续合约短线 `live` 的单主交易节点架构，围绕状态契约、治理快照、风控硬闸和恢复链路组织，实现快路径确定性执行与慢路径 AI 治理协同的 Trading Harness。**
