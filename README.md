# Bedrock 模型调用 · CloudWatch 监控一键部署

一条命令部署 Bedrock 模型调用的 CloudWatch 看板，告警可选。支持两种模式：

- **`generic`（通用调用监控）**：与调用方无关，监控本账号本区域对选定模型或全部模型的调用。
- **`chain`（降级链监控）**：用于"New API 等网关后挂多个模型、按顺序降级"的方案，看板和告警会标出降级顺序和兜底模型。

数据只来自 `AWS/Bedrock` 原生指标（按 ModelId），不需要部署 Lambda。

依赖：Python 3（只用标准库）、AWS CLI v2，并且已配置好凭证。

## 快速开始

```bash
cp config.example.json config.json   # 按需修改区域、模型、阈值
./deploy.sh list-models              # 查看本区域哪些 ModelId 有数据（* = 最近 3 小时有调用）
./deploy.sh                          # 部署看板（重复执行即更新）
./deploy.sh deploy --with-alarms --sns-email ops@example.com   # 同时创建告警并发邮件通知
./deploy.sh delete                   # 删除看板和告警
```

`deploy.sh` 会把参数原样传给 `deploy.py`，并且默认读取 `config.json`。

## 通用调用监控（与 New API 无关）

```bash
python3 deploy.py deploy -c config.generic.json                      # 自动纳入本区域所有有数据的模型
python3 deploy.py deploy -c config.generic.json --region us-east-1   # 换区域
python3 deploy.py deploy -c config.generic.json \
  --model "Opus 5.5=global.anthropic.claude-opus-5-5" --model "Nova 2 Lite=global.amazon.nova-2-lite-v1:0"   # 只看指定模型
python3 deploy.py deploy -c config.generic.json --with-alarms --sns-email ops@example.com
```

- `config.generic.json` 里写的是 `"mode": "generic"` 和 `"models": "auto"`。`auto` 会在**每次部署时**重新发现本区域有指标的 ModelId（CloudWatch 只返回最近两周有数据的指标），所以新模型上线后重新部署一次即可纳入。
- 也可以在任意配置上用 `--mode generic`、`--all-models` 临时切换。
- 通用模式下看板不显示降级链，告警描述统一标为"调用监控"，不区分"降一档 / 兜底"。
- 统计口径是本账号本区域对该模型的**全部调用**，不区分是哪个应用发起的。需要按应用区分时，要用不同账号，或者另外采集应用侧的自定义指标。

## 配置项（config.json）

| 字段 | 说明 |
|---|---|
| `mode` | `chain`（默认，降级链）或 `generic`（通用调用监控） |
| `region` | 部署区域，必须和发起 Bedrock 调用的区域一致（指标记在发起调用的区域，跨区域推理也一样） |
| `name` | 看板名，同时也是告警名和 SNS 主题名的前缀 |
| `title` | 说明文字里的标题（只在开启 `description` 时显示） |
| `description` | 默认 `false`，看板顶部不显示说明文字；设为 `true` 或加 `--description` 时才显示 |
| `period` / `range` | 指标周期（秒）/ 看板默认时间范围（如 `-PT3H`） |
| `models` | 填 `"auto"` 表示自动发现本区域全部模型；或者填数组，降级链模式下数组顺序即降级顺序：`label` 显示名、`model_id` 按 CloudWatch 中的 ModelId（跨区域推理带 `global.` 等前缀）、`channel` / `priority` 可选、只用于在看板说明里显示网关的渠道号和优先级（不影响监控和告警，一般不用填）、`ttft_p95_ms` 大于 0 时额外创建首字时延告警、`color` 可选 |
| `alarms.enabled` | 是否创建告警（也可以用 `--with-alarms` 开启） |
| `alarms.sns_topic_arn` / `sns_email` | 二选一：复用已有主题，或者自动创建 `<name>-alerts` 主题并订阅这个邮箱（需要点邮件里的确认链接） |
| `alarms.error_rate_pct` | "错误 + 限流"占比阈值（%） |
| `alarms.min_invocations` | 每个周期的调用量低于这个值时不判定，防止低流量误报 |
| `alarms.datapoints` / `evaluation_periods` | 例如 3 / 5，表示 5 个周期里有 3 个超标才告警 |

## 命令行覆盖（不改配置文件）

```bash
# 换区域、换名字、换模型链（--model 按降级顺序重复传入，会整体替换配置里的 models）
python3 deploy.py deploy -c config.json --region us-east-1 --name Prod-Chain \
  --model "GPT-6.1 Sol=global.openai.gpt-6.1-sol" \
  --model "Opus 5.5=global.anthropic.claude-opus-5-5"

python3 deploy.py deploy -c config.json --with-alarms --error-rate 3 --min-invocations 50
python3 deploy.py deploy -c config.json --profile prod      # 指定 AWS CLI profile
python3 deploy.py render -c config.json --with-alarms       # 只打印看板 / 告警 JSON，不调用 AWS
python3 deploy.py deploy -c config.json --dry-run           # 只打印将要执行的 AWS 命令
```

## 部署了什么

- **看板**：说明文字（默认关闭）、告警状态（启用告警时显示）、各模型调用次数、错误 + 限流占比（带阈值线）、首字时延 p95、总时延 p95、报错与限流次数、估算 TPM 用量、Token 用量、时间范围汇总。
- **告警**（可选，每个模型一个）：`(InvocationServerErrors + InvocationThrottles) / Invocations`，调用量不足时不判定；配了 `ttft_p95_ms` 的模型再加一个首字时延告警。降级链的最后一个模型在描述里标为"兜底，仅通知，转人工"。
- 告警只负责通知，**不会自动修改 New API 渠道**。自动降级、停用、恢复需要另外部署手册里的控制 Lambda。

## 安全边界

- 所有资源名都以 `name` 开头；告警和 SNS 主题带 `ManagedBy=bedrock-degrade-monitor` 标签。
- 重新部署时，只会清理**本工具创建**、并且已经不在配置里的告警。
- `delete` 只删除同名看板，以及带本工具标签的告警；加上 `--delete-topic` 才会删除 `<name>-alerts` 主题。

## 注意

- ModelId 写错或者模型在本区域从没被调用过，部署时会提示 ⚠，曲线也会是空的。可以先跑 `list-models` 核对。
- 原生指标只有在账号里只有 New API 调用这些模型时，才等于"渠道"指标；如果有其他应用调用同一个模型，数据会混在一起。
- 原生指标看不到"请求没到 Bedrock 的失败"和"停用渠道是否恢复"，这两项需要探测类自定义指标，见手册步骤 2-B。
- 费用：看板前 3 个免费，之后约 $3 / 月 / 个；告警按所含指标数计费（标准分辨率约 $0.10 / 指标 / 月），错误率告警含 3 个指标，约 $0.30 / 月 / 模型。具体以 CloudWatch 定价页为准。
