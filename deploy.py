#!/usr/bin/env python3
"""Bedrock 模型调用 CloudWatch 监控一键部署（通用监控 / 降级链监控两种模式）。

只依赖 Python 3 标准库 + 已配置好的 AWS CLI v2。
创建/更新：1 个 CloudWatch 看板；可选每个模型的告警（错误+限流占比、首字时延）和 SNS 通知。
所有资源名都以 <name> 开头，delete 只删除这些资源。

用法示例：
  python3 deploy.py list-models --region ap-northeast-1          # 看本区域有哪些 ModelId 有数据
  python3 deploy.py deploy -c config.generic.json --all-models   # 通用模式：自动纳入本区域所有有数据的模型
  python3 deploy.py deploy -c config.json                        # 按配置部署
  python3 deploy.py deploy -c config.json --region us-east-1 --name Prod-Chain \
      --model "GPT-6.1 Sol=global.openai.gpt-6.1-sol" --model "Opus 5.5=global.anthropic.claude-opus-5-5"
  python3 deploy.py deploy -c config.json --with-alarms --sns-email ops@example.com
  python3 deploy.py render -c config.json                        # 只打印将要提交的 JSON，不调用 AWS
  python3 deploy.py delete -c config.json                        # 删除看板、告警（SNS 主题需加 --delete-topic）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

TOOL_TAG = "bedrock-degrade-monitor"
NS = "AWS/Bedrock"
PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#d62728", "#8c564b", "#e377c2", "#17becf"]

DEFAULTS = {
    "region": os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-1",
    "name": "Bedrock-Degrade-Chain",
    "mode": "chain",
    "title": "",
    "period": 60,
    "range": "-PT3H",
    "description": False,
    "alarms": {
        "enabled": False, "sns_topic_arn": "", "sns_email": "",
        "error_rate_pct": 5, "min_invocations": 20, "datapoints": 3, "evaluation_periods": 5,
    },
    "models": [],
}


# ---------------------------------------------------------------- AWS CLI
class Aws:
    def __init__(self, region, profile=None, dry=False):
        self.region, self.profile, self.dry = region, profile, dry

    def __call__(self, *args, check=True):
        cmd = ["aws", *args, "--region", self.region, "--output", "json"]
        if self.profile:
            cmd += ["--profile", self.profile]
        if self.dry:
            print("[dry-run] " + " ".join(a if len(a) < 80 else a[:77] + "..." for a in cmd))
            return {}
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            if check:
                sys.exit(f"✗ AWS CLI 失败：{' '.join(args[:2])}\n{r.stderr.strip()}")
            return None
        return json.loads(r.stdout) if r.stdout.strip() else {}

    def with_file(self, args, flag, payload):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            path = f.name
        try:
            return self(*args, flag, f"file://{path}")
        finally:
            os.unlink(path)


# ---------------------------------------------------------------- config
def slug(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s or "model"


def load_config(a):
    cfg = json.loads(json.dumps(DEFAULTS))
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            user = json.load(f)
        alarms = {**cfg["alarms"], **user.pop("alarms", {})}
        cfg.update(user)
        cfg["alarms"] = alarms
    for k in ("region", "name", "title", "mode"):
        if getattr(a, k, None):
            cfg[k] = getattr(a, k)
    if getattr(a, "model", None):
        cfg["models"] = []
        for i, m in enumerate(a.model):
            label, _, mid = m.partition("=")
            if not mid:
                label, mid = m, m
            cfg["models"].append({"label": label.strip(), "model_id": mid.strip(), "channel": "", "priority": None})
    al = cfg["alarms"]
    if getattr(a, "with_alarms", False):
        al["enabled"] = True
    for k, attr in (("sns_topic_arn", "sns_topic_arn"), ("sns_email", "sns_email"),
                    ("error_rate_pct", "error_rate"), ("min_invocations", "min_invocations")):
        v = getattr(a, attr, None)
        if v not in (None, ""):
            al[k] = v
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", cfg["name"]):
        sys.exit("✗ name 只能包含字母、数字、- 和 _")
    if getattr(a, "description", False):
        cfg["description"] = True
    if getattr(a, "all_models", False):
        cfg["models"] = "auto"
    if cfg["mode"] not in ("chain", "generic"):
        sys.exit("✗ mode 只能是 chain（降级链）或 generic（通用监控）")
    cfg["title"] = cfg["title"] or ("Bedrock 模型降级链监控" if cfg["mode"] == "chain" else "Bedrock 模型调用监控")
    return cfg


def resolve_models(cfg, aws):
    """models 为 "auto" 时，取本区域所有有 Invocations 指标的 ModelId；然后补齐颜色、内部 key。"""
    if cfg["models"] == "auto":
        ids = sorted(active_model_ids(aws))
        if not ids:
            sys.exit(f"✗ {cfg['region']} 中没有任何 ModelId 的 Invocations 指标")
        cfg["models"] = [{"label": re.sub(r"^(global|us|eu|apac|jp|au|ca|in|us-gov)\.", "", i), "model_id": i} for i in ids]
        print(f"· 自动发现 {len(ids)} 个模型：" + "、".join(m["label"] for m in cfg["models"]), file=sys.stderr)
    if not cfg["models"]:
        sys.exit("✗ 没有配置模型：在 config 的 models 里填写，用 --model \"标签=ModelId\"，或用 --all-models")
    for i, m in enumerate(cfg["models"]):
        m.setdefault("label", m["model_id"])
        m["color"] = m.get("color") or PALETTE[i % len(PALETTE)]
        m["key"] = f"m{i}"


# ---------------------------------------------------------------- dashboard
def build_dashboard(cfg, alarm_arns):
    R, P, models = cfg["region"], cfg["period"], cfg["models"]

    def widget(x, y, w, h, props, kind="metric"):
        return {"type": kind, "x": x, "y": y, "width": w, "height": h, "properties": props}

    def ts(title, metrics, **kw):
        p = {"title": title, "region": R, "metrics": metrics, "view": "timeSeries", "period": P}
        p.update(kw)
        return p

    def per(metric, stat, suffix=""):
        return [[NS, metric, "ModelId", m["model_id"], {"label": m["label"] + suffix, "color": m["color"], "stat": stat}]
                for m in models]

    rate = []
    for m in models:
        k, mid = m["key"], m["model_id"]
        rate += [[NS, "Invocations", "ModelId", mid, {"id": "i" + k, "visible": False, "stat": "Sum"}],
                 [NS, "InvocationServerErrors", "ModelId", mid, {"id": "e" + k, "visible": False, "stat": "Sum"}],
                 [NS, "InvocationThrottles", "ModelId", mid, {"id": "t" + k, "visible": False, "stat": "Sum"}],
                 [{"expression": f"IF(i{k}>0, 100*(FILL(e{k},0)+FILL(t{k},0))/i{k}, 0)",
                   "label": m["label"], "id": "r" + k, "color": m["color"]}]]

    parts = []
    for m in models:
        extra = ([f"渠道 {m['channel']}"] if m.get("channel") else []) + \
                ([f"优先级 {m['priority']}"] if m.get("priority") is not None else [])
        parts.append(m["label"] + (f"（{'，'.join(extra)}）" if extra else ""))
    thr = cfg["alarms"]["error_rate_pct"]
    src = f"区域 `{R}` · 数据源 `AWS/Bedrock` 原生指标（按 ModelId）"
    if cfg["mode"] == "chain":
        text = (f"## {cfg['title']}\n**降级链：** {' → '.join(parts)} · {src}\n\n"
                f"**怎么看：** ① 流量分布看当前实际在用哪个模型；② 错误+限流占比是降一档的依据（阈值 {thr}%）；"
                f"⑥ 估算 TPM 用量对照配额，看下一级模型有没有余量。\n"
                "原生指标看不到\"请求没到 Bedrock 的失败\"和\"停用渠道是否恢复\"，这两项需要探测类自定义指标。"
                "没有调用记录的模型曲线为空属正常。")
    else:
        text = (f"## {cfg['title']}\n**监控模型：** {'、'.join(parts)} · {src}\n\n"
                f"**怎么看：** ① 各模型调用量；② 错误+限流占比（阈值 {thr}%）；③④ 首字 / 总时延 p95；"
                f"⑤ 报错与限流次数；⑥ 估算 TPM 用量对照配额看余量；⑦ Token 用量。\n"
                "统计的是本账号本区域对这些模型的全部调用（不区分调用方）。没有调用记录的模型曲线为空属正常。")

    # 说明文字默认不显示；配置 "description": true 或 --description 时才加
    W = [widget(0, 0, 24, 4, {"markdown": text}, "text")] if cfg.get("description") else []
    y = 4 if W else 0
    if alarm_arns:
        W.append(widget(0, y, 24, 3, {"title": "告警状态", "alarms": alarm_arns, "sortBy": "stateUpdatedTimestamp"}, "alarm"))
        y += 3
    W += [
        widget(0, y, 12, 6, ts("① 流量分布：各模型调用次数", per("Invocations", "Sum"), stacked=True)),
        widget(12, y, 12, 6, ts("② 错误 + 限流占比 %" + ("（降一档依据）" if cfg["mode"] == "chain" else ""), rate,
                                yAxis={"left": {"min": 0, "label": "%", "showUnits": False}},
                                annotations={"horizontal": [{"label": f"阈值 {thr}%", "value": thr, "color": "#d62728"}]})),
        widget(0, y + 6, 8, 6, ts("③ 首字时延 TimeToFirstToken p95（ms）", per("TimeToFirstToken", "p95"))),
        widget(8, y + 6, 8, 6, ts("④ 总时延 InvocationLatency p95（ms）", per("InvocationLatency", "p95"))),
        widget(16, y + 6, 8, 6, ts("⑤ 报错与限流次数", per("InvocationServerErrors", "Sum", " 5xx")
                                   + per("InvocationThrottles", "Sum", " 限流") + per("InvocationClientErrors", "Sum", " 4xx"))),
        widget(0, y + 12, 12, 6, ts("⑥ 估算 TPM 用量（tokens/分钟，对照配额看余量）", per("EstimatedTPMQuotaUsage", "Maximum"))),
        widget(12, y + 12, 12, 6, ts("⑦ Token 用量（每 5 分钟）", per("InputTokenCount", "Sum", " 输入")
                                     + per("OutputTokenCount", "Sum", " 输出"), period=300)),
        widget(0, y + 18, 24, 4, ts("⑧ 所选时间范围汇总", per("Invocations", "Sum", " 调用")
                                    + per("InvocationServerErrors", "Sum", " 5xx") + per("InvocationThrottles", "Sum", " 限流"),
                                    view="singleValue", period=3600, setPeriodToTimeRange=True)),
    ]
    return {"start": cfg["range"], "periodOverride": "inherit", "widgets": W}


# ---------------------------------------------------------------- alarms
def build_alarms(cfg, topic_arn):
    al, out = cfg["alarms"], []
    actions = [topic_arn] if topic_arn else []
    last = len(cfg["models"]) - 1
    for i, m in enumerate(cfg["models"]):
        mid, base = m["model_id"], f"{cfg['name']}-{slug(m['label'])}"
        role = ("兜底，仅通知，转人工" if i == last and last > 0 else "降一档依据") if cfg["mode"] == "chain" else "调用监控"
        stat = lambda metric, s="Sum": {"Metric": {"Namespace": NS, "MetricName": metric,
                                                   "Dimensions": [{"Name": "ModelId", "Value": mid}]},
                                        "Period": cfg["period"], "Stat": s}
        out.append({
            "AlarmName": f"{base}-error-rate",
            "AlarmDescription": f"[{role}] {m['label']}（{mid}）错误+限流占比 > {al['error_rate_pct']}%，"
                                f"{al['datapoints']}/{al['evaluation_periods']} 个周期；调用量 < {al['min_invocations']} 时不判定。",
            "Metrics": [
                {"Id": "inv", "MetricStat": stat("Invocations"), "ReturnData": False},
                {"Id": "err", "MetricStat": stat("InvocationServerErrors"), "ReturnData": False},
                {"Id": "thr", "MetricStat": stat("InvocationThrottles"), "ReturnData": False},
                {"Id": "rate", "Label": "error+throttle %", "ReturnData": True,
                 "Expression": f"IF(inv>={al['min_invocations']}, 100*(FILL(err,0)+FILL(thr,0))/inv, 0)"},
            ],
            "ComparisonOperator": "GreaterThanThreshold", "Threshold": float(al["error_rate_pct"]),
            "EvaluationPeriods": int(al["evaluation_periods"]), "DatapointsToAlarm": int(al["datapoints"]),
            "TreatMissingData": "missing", "AlarmActions": actions, "OKActions": actions,
        })
        if m.get("ttft_p95_ms"):
            out.append({
                "AlarmName": f"{base}-ttft-p95",
                "AlarmDescription": f"[{role}] {m['label']} 首字时延 p95 > {m['ttft_p95_ms']} ms。",
                "Metrics": [{"Id": "ttft", "MetricStat": stat("TimeToFirstToken", "p95"), "ReturnData": True}],
                "ComparisonOperator": "GreaterThanThreshold", "Threshold": float(m["ttft_p95_ms"]),
                "EvaluationPeriods": int(al["evaluation_periods"]), "DatapointsToAlarm": int(al["datapoints"]),
                "TreatMissingData": "missing", "AlarmActions": actions, "OKActions": actions,
            })
    return out


# ---------------------------------------------------------------- commands
def check_models(aws, cfg):
    found = active_model_ids(aws)
    for m in cfg["models"]:
        mark = "✓" if m["model_id"] in found else "⚠ 本区域暂无该 ModelId 的指标（未调用过或 ID 写错）"
        print(f"  {m['label']:<20} {m['model_id']:<45} {mark}")


def active_model_ids(aws):
    d = aws("cloudwatch", "list-metrics", "--namespace", NS, "--metric-name", "Invocations")
    return {x["Value"] for m in d.get("Metrics", []) for x in m["Dimensions"] if x["Name"] == "ModelId"}


def ensure_topic(aws, cfg):
    al = cfg["alarms"]
    if al.get("sns_topic_arn"):
        return al["sns_topic_arn"]
    if not al.get("sns_email"):
        return ""
    arn = aws("sns", "create-topic", "--name", f"{cfg['name']}-alerts",
              "--tags", f"Key=ManagedBy,Value={TOOL_TAG}", f"Key=Stack,Value={cfg['name']}").get("TopicArn", "")
    if arn and not aws.dry:
        subs = aws("sns", "list-subscriptions-by-topic", "--topic-arn", arn).get("Subscriptions", [])
        if not any(s.get("Endpoint") == al["sns_email"] for s in subs):
            aws("sns", "subscribe", "--topic-arn", arn, "--protocol", "email", "--notification-endpoint", al["sns_email"])
            print(f"  ✉ 已向 {al['sns_email']} 发送订阅确认邮件，需点击确认后才会收到告警")
    return arn


def console_url(cfg):
    r = cfg["region"]
    return f"https://{r}.console.aws.amazon.com/cloudwatch/home?region={r}#dashboards/dashboard/{cfg['name']}"


def cmd_deploy(aws, cfg):
    print(f"▶ 部署到 {cfg['region']}，名称前缀 {cfg['name']}")
    if not aws.dry:
        print("· 检查模型指标："); check_models(aws, cfg)
    alarm_arns = []
    if cfg["alarms"]["enabled"]:
        topic = ensure_topic(aws, cfg)
        if not topic:
            print("  ⚠ 未配置 sns_topic_arn / sns_email，告警只显示状态、不发通知")
        acct = "" if aws.dry else aws("sts", "get-caller-identity")["Account"]
        for alarm in build_alarms(cfg, topic):
            aws.with_file(["cloudwatch", "put-metric-alarm", "--alarm-name", alarm["AlarmName"],
                           "--tags", f"Key=ManagedBy,Value={TOOL_TAG}", f"Key=Stack,Value={cfg['name']}",
                           "--alarm-description", alarm["AlarmDescription"],
                           "--comparison-operator", alarm["ComparisonOperator"],
                           "--threshold", str(alarm["Threshold"]),
                           "--evaluation-periods", str(alarm["EvaluationPeriods"]),
                           "--datapoints-to-alarm", str(alarm["DatapointsToAlarm"]),
                           "--treat-missing-data", alarm["TreatMissingData"],
                           *(["--alarm-actions", *alarm["AlarmActions"], "--ok-actions", *alarm["OKActions"]]
                             if alarm["AlarmActions"] else [])],
                          "--metrics", alarm["Metrics"])
            alarm_arns.append(f"arn:aws:cloudwatch:{cfg['region']}:{acct}:alarm:{alarm['AlarmName']}")
            print(f"  ✓ 告警 {alarm['AlarmName']}")
        stale = {a["AlarmName"] for a in list_own_alarms(aws, cfg)} - {x.rsplit(':', 1)[1] for x in alarm_arns}
        if stale and not aws.dry:
            aws("cloudwatch", "delete-alarms", "--alarm-names", *sorted(stale))
            print(f"  − 清理已不在配置里的告警：{', '.join(sorted(stale))}")
    r = aws.with_file(["cloudwatch", "put-dashboard", "--dashboard-name", cfg["name"]],
                      "--dashboard-body", build_dashboard(cfg, alarm_arns))
    for msg in (r or {}).get("DashboardValidationMessages", []):
        print("  ⚠ " + msg.get("Message", str(msg)))
    print(f"✓ 看板已更新：{console_url(cfg)}")


def list_own_alarms(aws, cfg):
    d = aws("cloudwatch", "describe-alarms", "--alarm-name-prefix", cfg["name"] + "-", "--alarm-types", "MetricAlarm") or {}
    own = []
    for a in d.get("MetricAlarms", []):
        tags = (aws("cloudwatch", "list-tags-for-resource", "--resource-arn", a["AlarmArn"]) or {}).get("Tags", [])
        if {"Key": "ManagedBy", "Value": TOOL_TAG} in tags:
            own.append(a)
    return own


def cmd_delete(aws, cfg, delete_topic):
    print(f"▶ 删除 {cfg['region']} 中名称前缀为 {cfg['name']} 的看板与告警（只删本工具创建、带 ManagedBy 标签的告警）")
    names = [a["AlarmName"] for a in list_own_alarms(aws, cfg)]
    if names:
        aws("cloudwatch", "delete-alarms", "--alarm-names", *names)
        print("  ✓ 已删除告警：" + ", ".join(names))
    if aws("cloudwatch", "get-dashboard", "--dashboard-name", cfg["name"], check=False) is not None:
        aws("cloudwatch", "delete-dashboards", "--dashboard-names", cfg["name"])
        print(f"  ✓ 已删除看板 {cfg['name']}")
    if delete_topic:
        acct = aws("sts", "get-caller-identity")["Account"]
        arn = f"arn:aws:sns:{cfg['region']}:{acct}:{cfg['name']}-alerts"
        if aws("sns", "get-topic-attributes", "--topic-arn", arn, check=False) is not None:
            aws("sns", "delete-topic", "--topic-arn", arn)
            print(f"  ✓ 已删除 SNS 主题 {arn}")


def cmd_list_models(aws):
    d = aws("cloudwatch", "list-metrics", "--namespace", NS, "--metric-name", "Invocations", "--recently-active", "PT3H")
    recent = {x["Value"] for m in d.get("Metrics", []) for x in m["Dimensions"] if x["Name"] == "ModelId"}
    ids = sorted(active_model_ids(aws))
    print(f"{aws.region} 中有 Invocations 指标的 ModelId（* = 最近 3 小时有调用）：")
    for i in ids:
        print(f"  {'*' if i in recent else ' '} {i}")
    if not ids:
        print("  （无）")


def main():
    ap = argparse.ArgumentParser(description="Bedrock 模型调用 CloudWatch 监控一键部署",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("cmd", choices=["deploy", "render", "delete", "list-models"])
    ap.add_argument("-c", "--config", help="配置文件（JSON），参见 config.example.json")
    ap.add_argument("--region"); ap.add_argument("--name", help="看板名，也是告警 / SNS 名称前缀")
    ap.add_argument("--title"); ap.add_argument("--profile", help="AWS CLI profile")
    ap.add_argument("--model", action="append", help="重复传入 \"标签=ModelId\"（降级链模式下顺序即降级顺序），会覆盖配置里的 models")
    ap.add_argument("--mode", choices=["chain", "generic"], help="chain=降级链（默认），generic=通用调用监控")
    ap.add_argument("--all-models", action="store_true", help="自动纳入本区域所有有指标的 ModelId（覆盖配置里的 models）")
    ap.add_argument("--description", action="store_true", help="在看板顶部显示说明文字（默认不显示）")
    ap.add_argument("--with-alarms", action="store_true", help="同时创建告警")
    ap.add_argument("--sns-topic-arn"); ap.add_argument("--sns-email", help="自动创建 SNS 主题并订阅该邮箱")
    ap.add_argument("--error-rate", type=float, help="错误+限流占比阈值 %%")
    ap.add_argument("--min-invocations", type=int, help="判定所需最小调用量")
    ap.add_argument("--delete-topic", action="store_true", help="delete 时一并删除本工具创建的 SNS 主题")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的 AWS 命令")
    a = ap.parse_args()
    if a.cmd == "list-models":
        region = a.region or (json.load(open(a.config, encoding="utf-8")).get("region") if a.config else DEFAULTS["region"])
        return cmd_list_models(Aws(region, a.profile))
    cfg = load_config(a)
    aws = Aws(cfg["region"], a.profile, a.dry_run)
    if a.cmd in ("deploy", "render"):
        resolve_models(cfg, Aws(cfg["region"], a.profile) if cfg["models"] == "auto" else aws)
    if a.cmd == "render":
        al = build_alarms(cfg, "<sns-topic-arn>") if cfg["alarms"]["enabled"] else []
        print(json.dumps({"dashboard": build_dashboard(cfg, [x["AlarmName"] for x in al]), "alarms": al},
                         ensure_ascii=False, indent=2))
        return
    if a.cmd == "deploy":
        cmd_deploy(aws, cfg)
    else:
        cmd_delete(aws, cfg, a.delete_topic)


if __name__ == "__main__":
    main()
