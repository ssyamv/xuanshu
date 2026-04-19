# Governor Research Provider 设计

## 1. 文档定位

本文档用于在 `Governor` 内部 `Strategy Research` 设计基础上，明确研究模型调用层的 provider 方案。

本文档只回答一个问题：

**当系统需要调用大模型参与研究分析时，正式支持哪些调用方式，以及它们在系统中的职责边界是什么。**

本文档承接：

- [2026-04-19-governor-strategy-research-design.md](/Users/chenqi/code/xuanshu/docs/superpowers/specs/2026-04-19-governor-strategy-research-design.md)

## 2. 核心结论

`Governor` 内部 `Strategy Research` 正式只支持两种 provider：

- `api`
- `codex_cli`

明确不支持：

- `chatgpt_pro_web`
- 网页自动化
- 网页前端接口抓取
- 无头浏览器模拟 ChatGPT 页面行为

## 3. Provider 架构定位

`Strategy Research` 的模型调用层只属于慢路径研究辅助能力，不属于：

- `Trader` 热路径
- 最终审批层
- 最终回测验证层

模型 provider 只负责：

- 研究假设生成
- 候选策略结构化建议
- 参数搜索思路整理
- 研究结果解释

模型 provider 不负责：

- 替代回测引擎
- 直接生成可执行交易动作
- 绕过 `Decision Committee`
- 直接发布 `StrategyConfigSnapshot`

## 4. Provider 类型

### 4.1 `api`

`api` 是正式长期方案。

特点：

- 使用 OpenAI API
- 适合作为长期稳定集成方式
- 可替代 `codex_cli`
- 适合未来做真正自动化研究链

### 4.2 `codex_cli`

`codex_cli` 是当前阶段的自动化替代方案。

特点：

- 通过本地 `codex` 命令行登录态调用模型能力
- 比网页自动化更稳定
- 仍然不是正式 API，但工程可控性高于网页方案
- 适合在尚无 API billing 时先接入自动研究辅助能力

边界：

- 只能作为 `Governor` 的 research helper
- 失败时必须降级为研究失败，不得阻塞治理主链
- 依赖服务器上的 `codex login` 登录态

## 5. 配置方式

provider 通过配置切换，不做自动回退。

建议配置：

```dotenv
XUANSHU_RESEARCH_PROVIDER=api
```

或：

```dotenv
XUANSHU_RESEARCH_PROVIDER=codex_cli
```

不采用：

- API 失败自动切换到 `codex_cli`
- `codex_cli` 失败自动切换到网页方案

原因：

- 自动回退会让审计链变得不透明
- 不同 provider 的行为、延迟、错误模式不同
- 研究结果来源应当明确可追踪

## 6. 运行原则

### 6.1 失败降级

无论 `api` 还是 `codex_cli`，provider 失败都只能导致：

- 本次 research 任务失败
- 记录研究失败事实
- 不产出研究候选包

而不能导致：

- `Trader` 停摆
- `Governor` 主循环崩溃
- 未审批研究结果进入快照

### 6.2 审批边界

provider 输出只是 research input，不是正式决策。

正式链路始终是：

`provider -> Strategy Research -> backtest/validation -> Decision Committee -> Snapshot Publisher -> Trader`

### 6.3 审计要求

系统必须能明确记录：

- 使用的是哪个 provider
- provider 调用是否成功
- 研究结果是否进入候选
- 候选是否通过审批

## 7. 为什么放弃 `chatgpt_pro_web`

放弃网页方案的原因：

- 不稳定
- 易受网页结构、登录态、验证码、风控变化影响
- 不适合作为正式系统依赖
- 审计与维护成本过高

因此，网页自动化不进入正式架构。

## 8. 本阶段结论

`Governor` 内部 `Strategy Research` 的 provider 方案正式收敛为：

- `api`
- `codex_cli`

其中：

- `api` 是长期正式方案
- `codex_cli` 是当前阶段的自动化替代方案

这使系统在没有 API billing 的阶段，仍然可以保留自动化研究辅助能力，同时避免把正式架构建立在网页自动化这种脆弱基础上。
