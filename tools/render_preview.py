#!/usr/bin/env python3
"""把看板渲染成一张预览图（用于 README）。

用 CloudWatch GetMetricWidgetImage 按真实数据逐个渲染图表，再按看板布局拼成一张 PNG。
CloudWatch 服务端出图不支持中文，所以图内标题留空、由本脚本用本地中文字体补画，
图例里的中文后缀替换成英文。

依赖：Pillow（pip install pillow）、AWS CLI v2。
用法：
  python3 tools/render_preview.py -c config.json --region us-east-1 --range -P7D -o docs/preview.png
"""
import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import deploy  # noqa: E402

LABEL_EN = {" 限流": " throttle", " 输入": " input", " 输出": " output", " 调用": " calls"}
FONTS = ["/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"]


def font(size):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    sys.exit("✗ 找不到中文字体，请在 FONTS 里补一个本机字体路径")


def en_labels(metrics):
    for row in metrics:
        opt = row[-1] if isinstance(row[-1], dict) else None
        if opt and "label" in opt:
            for zh, en in LABEL_EN.items():
                opt["label"] = opt["label"].replace(zh, en)
    return metrics


def range_seconds(rng):
    m = re.fullmatch(r"-P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", rng)
    if not m:
        sys.exit("✗ --range 格式应类似 -PT6H / -P1D / -P7D")
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60


def widget_png(props, w, h, region, rng, period, profile):
    p = dict(props, title="", width=w, height=h, start=rng, period=max(period, props.get("period", 60)),
             metrics=en_labels(json.loads(json.dumps(props["metrics"]))))
    if "annotations" in props:  # 阈值线标签也要去中文
        p["annotations"] = {"horizontal": [dict(a, label=f"threshold {a['value']}%") for a in props["annotations"]["horizontal"]]}
    cmd = ["aws", "cloudwatch", "get-metric-widget-image", "--region", region, "--output", "json",
           "--metric-widget", json.dumps(p, ensure_ascii=False)]
    if profile:
        cmd += ["--profile", profile]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(f"  ⚠ {props.get('title')} 渲染失败：{r.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return Image.open(io.BytesIO(base64.b64decode(json.loads(r.stdout)["MetricWidgetImage"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("--region"); ap.add_argument("--profile")
    ap.add_argument("--range", default="-P1D", help="渲染时间范围，如 -PT6H / -P1D / -P7D（注意写成 --range=-P7D）")
    ap.add_argument("--period", type=int, help="出图聚合周期（秒），默认按时间范围自动选，让稀疏数据也连成线")
    ap.add_argument("--all-models", action="store_true")
    ap.add_argument("--mode", choices=["chain", "generic"])
    ap.add_argument("-o", "--output", default=os.path.join(os.path.dirname(HERE), "docs", "preview.png"))
    a = ap.parse_args()

    ns = argparse.Namespace(cmd="render", config=a.config, region=a.region, name=None, title=None, mode=a.mode,
                            model=None, with_alarms=False, sns_topic_arn=None, sns_email=None, error_rate=None,
                            min_invocations=None, all_models=a.all_models, description=False)
    cfg = deploy.load_config(ns)
    deploy.resolve_models(cfg, deploy.Aws(cfg["region"], a.profile))
    # 汇总数字组件（singleValue）服务端出图不支持，预览里略过
    widgets = [w for w in deploy.build_dashboard(cfg, [])["widgets"]
               if w["type"] == "metric" and w["properties"].get("view") != "singleValue"]
    secs = range_seconds(a.range)
    period = a.period or (60 if secs <= 3 * 3600 else 300 if secs <= 86400 else 900 if secs <= 3 * 86400 else 3600)

    COL, ROW, PAD, TITLE_H = 70, 50, 10, 34          # 24 列栅格，每格 70px；每行 50px
    W = 24 * COL + PAD
    H = max(w["y"] + w["height"] for w in widgets) * ROW + PAD + 70
    canvas = Image.new("RGB", (W, H), "#f2f3f3")
    d = ImageDraw.Draw(canvas)
    d.text((PAD + 6, 18), f"{cfg['title']} · {cfg['region']} · 最近 {a.range[2:].replace('T', '')} · 聚合周期 {period}s",
           fill="#16191f", font=font(24))
    top = 70
    for w in widgets:
        x0, y0 = PAD + w["x"] * COL, top + w["y"] * ROW
        pw, ph = w["width"] * COL - PAD, w["height"] * ROW - PAD
        d.rectangle([x0, y0, x0 + pw, y0 + ph], fill="white", outline="#d5dbdb")
        d.text((x0 + 12, y0 + 8), w["properties"]["title"], fill="#16191f", font=font(16))
        img = widget_png(w["properties"], pw - 4, ph - TITLE_H - 4, cfg["region"], a.range, period, a.profile)
        if img:
            canvas.paste(img, (x0 + 2, y0 + TITLE_H))
        print(f"  ✓ {w['properties']['title']}", file=sys.stderr)
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    canvas.save(a.output, optimize=True)
    print(f"✓ 预览图：{a.output}（{W}×{H}）")


if __name__ == "__main__":
    main()
