# Kiro 可观测性：接入 Amazon CloudWatch

[English](README.md)

一个 CloudFormation 堆栈，把 Kiro Enterprise 的用量数据送进 Amazon CloudWatch，并附带一个现成的看板：

- **实时事件**（每次 LLM 请求、每次工具调用各一条）写入 CloudWatch Logs 日志组。
- **每日指标**（`kiro.daily.*`，和 CSV 日报是同一份日总量）写入 CloudWatch OTLP 指标。
- 堆栈建好 AWS 侧需要的全部资源，**自动轮换 API key**，并且**在 Kiro Controls 策略里打开这两个导出**，只改这几项设置。

基于 Kiro 的 [实时事件和私网连接](https://kiro.dev/changelog/general/real-time-telemetry-and-private-connectivity/)
和 [OpenTelemetry 指标](https://kiro.dev/docs/enterprise/monitor-and-track/user-activity/opentelemetry/) 功能。

## 数据怎么流动

```
Kiro IDE / CLI / Web ──► Kiro 服务（AWS 托管）
                            │ 从你的 Controls 策略里读到密钥 ARN
                            ▼
                Secrets Manager 密钥（在你的账号里，KMS 加密）
                  = OTLP 地址 + "Authorization: Bearer <CloudWatch API key>"
                            │ OTLP over HTTPS
                            ▼
      CloudWatch Logs  /kiro/otel-events        CloudWatch OTLP 指标  kiro.daily.*
      （实时事件）                               （每日指标）
                            ▼
                   CloudWatch 看板 "Kiro-Usage"
```

数据由 Kiro 服务端发出，不是开发者电脑发出，所以电脑上什么都不用装。
导出的是用量数据（用户 ID 和邮箱、模型、credit、耗时、工具名和结果），不含提示词、回答、代码或文件内容。

## 堆栈会建什么

| 资源 | 用途 |
|---|---|
| KMS 密钥 `alias/<NamePrefix>-export` | 加密两个密钥；策略只允许 Kiro（`q.amazonaws.com`）通过 Secrets Manager 解密 |
| 日志组 `/kiro/otel-events` 和日志流 `events` | 接收实时事件；开启 bearer token 认证；保留 30 天；删除堆栈时保留 |
| IAM 用户 `<NamePrefix>-logs`、`<NamePrefix>-metrics` | 每把 API key 一个只写身份（日志用户只能写这个日志组） |
| 密钥 `<NamePrefix>/events`、`<NamePrefix>/metrics` | Kiro 读取的内容：地址和 API key。资源策略只允许 Kiro 读 |
| Lambda `KeyFunction` 和两个轮换计划 | 创建 API key，每 30 天轮换一次 |
| Lambda `KiroPolicyFunction` | 把 7 个导出设置合并进 Kiro Controls 策略，检查后发布 |
| 看板 `Kiro-Usage` 和 5 条保存的 Logs Insights 查询 | 查看数据 |

## 前提条件

- Kiro Enterprise，已使用 **Controls**（Kiro Enterprise Management），profile 在 **美东（弗吉尼亚北部）** 或 **欧洲（法兰克福）**。实时事件只支持这两个地区。
- 部署到 **Kiro profile 所在的同一个 AWS 账号和地区**。
- 有权限创建 IAM 用户和角色、KMS 密钥、密钥、Lambda 和 CloudWatch 资源（`CAPABILITY_NAMED_IAM`），并能发布 Kiro Controls 策略。
- 客户端：每日指标和 LLM 请求事件所有客户端都有。工具调用事件需要 Kiro CLI 3.0.0 以上或最新的 Kiro IDE；升级后要重启所有 IDE 窗口和 `kiro-cli` 进程。

## 部署

**控制台：** CloudFormation → Create stack → Upload a template file → 选 `template/kiro-observability.yaml`。保持默认值，勾选 IAM 资源确认，创建。打开输出里的 `DashboardUrl`。

**命令行：**

```bash
git clone <本仓库> && cd kiro-observability-cloudwatch
REGION=us-east-1 ./scripts/deploy.sh
```

`deploy.sh` 会新建或更新堆栈（默认名 `kiro-observability`），并打印输出。参数用 `Key=Value` 传；更新时没传的参数沿用当前值。

```bash
REGION=us-east-1 ./scripts/deploy.sh ConfigureKiroPolicy=false
REGION=us-east-1 ./scripts/deploy.sh CreateLogGroup=false LogGroupName=/my/existing/group
```

### 参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `NamePrefix` | `kiro-otel` | KMS 别名、IAM 用户和密钥名的前缀 |
| `DashboardName` | `Kiro-Usage` | 看板名，也是保存查询所在的文件夹名 |
| `CreateLogGroup` | `true` | `false` = 用已有的日志组（必须已开 bearer token 认证并有日志流） |
| `LogGroupName` / `LogStreamName` | `/kiro/otel-events` / `events` | 实时事件写到哪里 |
| `RetentionInDays` | `30` | 日志保留天数 |
| `ConfigureKiroPolicy` | `true` | `false` = 用输出 `KglSettings` 自己去 Controls 里加 |
| `KiroPolicyId` | 空 | Controls 策略 ID。空 = 自动选分配给 Kiro profile 的那一份 |
| `KiroProfileId` | 空 | 有多个 profile 的策略时，或配合 `KiroIdentityStoreId` 时需要 |
| `KiroIdentityStoreId` | 空 | 只有策略规则里用了用户或用户组 `when` 条件时才需要 |
| `KiroOnDelete` | `disable` | 删除堆栈时关掉两个导出（只在它们还指向本堆栈的密钥时） |
| `AllowExistingDraft` | `false` | `true` = 草稿里有别人没发布的改动也照样发布 |

### 输出

| 输出 | 内容 |
|---|---|
| `DashboardUrl` | 看板链接 |
| `EventsSecretArn`、`MetricsSecretArn` | Kiro 读取的两个密钥 |
| `KiroPolicyId`、`KiroPolicyResult` | 堆栈配置的策略，以及发布的版本号（或 `unchanged`） |
| `KglSettings` | 堆栈写入的设置，手动配置时用 |
| `KeyRotation` | 轮换周期 |

## 堆栈会改 Kiro Controls 策略里的什么

只改策略 KGL 文件 `settings:` 下的这几个键。规则、其他设置、注释和 YAML 标签都不动。

```yaml
settings:
  user_activity_report.enabled: true                 # 即 "Collect metrics per user"，每日导出需要
  user_activity_report.otel_export: true
  user_activity_report.otel_secret_arn: "<MetricsSecretArn>"
  user_activity_report.otel_protocol: http/protobuf  # CloudWatch 不收 gRPC
  user_activity_report.otel_temporality: delta       # 必须小写；写 "Delta" 会检查失败
  realtime_event_export.enabled: true
  realtime_event_export.secret_arn: "<EventsSecretArn>"
```

- 策略里已经是这些值时不会发布（`KiroPolicyResult` = `unchanged`）。
- 草稿里有别人没发布的改动时，堆栈会失败，不会把那些改动一起发布出去。
- 发布后约 5 分钟内对该 profile 的所有用户生效。
- 撤销发布：Kiro 控制台 → Controls → Actions → Revision history，或用
  `aws kiro-em restore-enterprise-management-resource`（见 [API 参考](https://kiro.dev/docs/enterprise/enterprise-management/api/)）。

## 验证

1. 在 Kiro IDE 或 CLI 里发一次请求。
2. 打开看板，或在 CloudWatch → **Logs → Log Analytics** 里选日志组 `/kiro/otel-events`，运行：
   ```
   filter eventName = "kiro.chat.message"
   | stats count(*) by attributes.kiro.model.name
   ```
   几秒内就有记录。
3. 每日指标在下一个 00:30 UTC 之后出现（2026-11-26 之前是 02:00 UTC）。它们不在 *All metrics* 页面里；
   请到 CloudWatch → **Query Studio** → **PromQL** 查询，例如 `{"kiro.daily.credits"}`。带点号的指标名必须加引号放进花括号。

Kiro 发送失败时不会通知你，也不会补发导出打开之前的数据。要看更早的用量，请打开 CSV 日报。

## 看板

| 部分 | 内容 |
|---|---|
| 总览 | credit、超额、请求数、活跃用户、会话数、工具调用数（按所选时间范围） |
| 用户 | 每人用量（档位、额度），每人每天 credit |
| 模型和客户端 | 各模型 credit，各模型 p50/p95 响应时间和被截断的回答，客户端和版本 |
| 趋势 | 每小时 credit，每小时请求数（按客户端） |
| 会话 | 最贵的会话，Agent 模式和子代理，按请求数分布的会话 |
| 工具 | 按工具、类型和结果统计的调用；MCP 服务器和错误 |
| 明细 | 最近 100 条请求 |
| 每日指标 | 最近一天的数字；每天按人、按模型的 credit；按客户端的消息；平均响应时间；客户端版本；Top 工具 |

每日指标的图都按一天一个窗口来算，因为 CloudWatch 会把 PromQL 的时间范围也算进 31 天的查询上限。

## API key 自动轮换

Secrets Manager 每 **30 天** 轮换一次 API key，每把 key **90 天** 后过期：

1. 先建一把新 key，并用它调用一次 CloudWatch 接口确认能用。
2. 密钥的当前值换成新 key。密钥 ARN 不变，所以 Kiro 那边什么都不用改。
3. 上一把 key 保留到下一次轮换，所以 Kiro 刚刚用旧 key 签名的请求也能成功。
4. 下一次轮换时删掉最旧的 key（IAM 每个用户每种服务最多两把 key）。

Kiro 每次发送都会重新读密钥，不做缓存。想马上轮换：
`aws secretsmanager rotate-secret --secret-id <EventsSecretArn>`。

## 更新和删除

- 更新：再运行一次 `./scripts/deploy.sh`，或用新模板更新堆栈。
- 删除：`aws cloudformation delete-stack --stack-name kiro-observability`。在 `KiroOnDelete=disable` 时，堆栈会先在 Kiro 策略里关掉两个导出，
  再删除 API key、用户、密钥、函数和看板。日志组和其中的数据会保留。KMS 密钥会进入计划删除。

## 安全说明

- API key 是持有者凭证，只存在密钥里，从不打印、记录日志或作为输出返回。
- 能读到它的：Kiro（通过密钥的资源策略），以及你账号里对这些资源有 `secretsmanager:GetSecretValue` 和 `kms:Decrypt` 权限的身份。请限制这些权限。
- 日志 key 只能写这一个日志组。指标 key 可以写这个账号里的任何指标（`cloudwatch:PutMetricData` 不支持限定资源）。
- Kiro 策略函数的角色按操作限权，资源是 `*`：IAM 目前不接受 Kiro Enterprise Management 预览接口的资源 ARN。
- 导出包含用户邮箱和 ID，请限制谁能读这个日志组和查询这些指标。

## 费用

你需要支付：Secrets Manager 和 KMS 请求费（Kiro 每次发送都读一次密钥，繁忙时大约每 5 秒两次）、CloudWatch Logs 写入和存储、
OTLP 指标序列（每个用户和每个细分值各一条）、看板运行的 Logs Insights 查询，以及两个小 Lambda。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `KiroPolicy` 失败："unpublished draft changes" | Controls 里有人留了草稿 | 先发布或丢弃它，或设 `AllowExistingDraft=true` |
| `KiroPolicy` 失败："N policies assigned to a profile" | 有多个 profile | 设 `KiroPolicyId` 或 `KiroProfileId` |
| `KiroPolicy` 失败：`when` 规则检查出错 | 策略用了用户或用户组条件 | 设 `KiroIdentityStoreId` 和 `KiroProfileId` |
| 堆栈成功但没有事件 | 策略还没生效，或没有人在用 | 等约 5 分钟，再发一次 Kiro 请求 |
| 有 LLM 事件，没有 `kiro.tool.call` | 客户端太旧 | 升级到 Kiro CLI 3.0.0+ 或最新 IDE，然后重启 |
| 每日指标的图是空的 | 部署后还没到每日导出时间 | 等到 00:30 UTC |
| 找不到 Logs Insights | 2026 年 6 月起并入了 Log Analytics | CloudWatch → Logs → Log Analytics |

## 开发

模板是生成的，这样看板 JSON 和 Lambda 代码都保持可读：

```bash
python3 src/build_template.py template > template/kiro-observability.yaml
python3 -m unittest discover -s tests      # 需要 PyYAML
```

测试会检查：提交的模板是最新的；模板小于 51,200 字节的内联上限；看板和 Lambda 代码能解析；PromQL 窗口不超过 31 天上限；
策略合并不会动到其他内容。

## 参考

- Kiro：[Monitor per-user activity](https://kiro.dev/docs/enterprise/monitor-and-track/user-activity/)、
  [Real-time events](https://kiro.dev/docs/enterprise/monitor-and-track/user-activity/real-time-events/)、
  [OpenTelemetry metrics](https://kiro.dev/docs/enterprise/monitor-and-track/user-activity/opentelemetry/)、
  [Prepare credentials](https://kiro.dev/docs/enterprise/monitor-and-track/user-activity/prepare-credentials/)、
  [Turn on exports](https://kiro.dev/docs/enterprise/monitor-and-track/user-activity/turn-on/)、
  [Enterprise Management](https://kiro.dev/docs/enterprise/enterprise-management/)、
  [KGL reference](https://kiro.dev/docs/enterprise/enterprise-management/kgl/)
- AWS：[CloudWatch Logs bearer token 认证](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_HTTP_Endpoints_BearerTokenAuth.html)、
  [CloudWatch 指标 bearer token 认证](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLP-MetricsBearerTokenAuth.html)、
  [PromQL 查询](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-PromQL-Querying.html)、
  [Log Analytics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/LogAnalytics.html)、
  [Secrets Manager 轮换](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotating-secrets.html)
