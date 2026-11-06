#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阿里云 ECS · 交易主机开通与运维（纯标准库，无 SDK 依赖）

凭据只从环境变量读，绝不落盘、绝不进 git：
    export ALIYUN_ACCESS_KEY_ID=xxx
    export ALIYUN_ACCESS_KEY_SECRET=xxx

命令：
    check                 验证 AK 是否有效、能看哪些地域（只读、免费）
    types                 列出可用实例规格（挑配置用）
    images                列出 Windows 公共镜像（拿 ImageId）
    price                 询真实价格（不创建，不扣费）
    create                创建实例 —— 默认 dry-run，必须加 --yes 才真的花钱
    status                查实例状态 / 公网 IP
    cmd "<powershell>"    云助手远程执行命令（不用远程桌面）
    deploy                把看门狗文件送进实例并自动部署（分块 base64）

为什么不用 SDK：这台机器上不一定装得了对的 SDK，而签名算法就十几行。
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib import parse

try:
    import urllib.request as urlreq
    import urllib.error as urlerr
except ImportError:                                   # pragma: no cover
    import urllib2 as urlreq                          # type: ignore

ENDPOINT = "https://ecs.aliyuncs.com"
API_VERSION = "2014-05-26"

# ══════════════════════ 签名 ══════════════════════

def pct(s):
    """阿里云要求：空格编成 %20（不是 +），保留 -_.~"""
    return parse.quote(str(s), safe="-_.~")


def call(ak, action, params=None, extra=None, timeout=30):
    p = {
        "Action": action,
        "Format": "JSON",
        "Version": API_VERSION,
        "AccessKeyId": ak["id"],
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if params:
        p.update({k: v for k, v in params.items() if v not in (None, "")})
    if extra:
        p.update(extra)

    cano = "&".join("%s=%s" % (pct(k), pct(p[k])) for k in sorted(p))
    sts = "GET&%s&%s" % (pct("/"), pct(cano))
    sig = base64.b64encode(
        hmac.new((ak["secret"] + "&").encode("utf-8"),
                 sts.encode("utf-8"), hashlib.sha1).digest()).decode()
    p["Signature"] = sig

    url = ENDPOINT + "/?" + "&".join("%s=%s" % (pct(k), pct(v)) for k, v in p.items())
    try:
        raw = urlreq.urlopen(url, timeout=timeout).read().decode("utf-8")
    except urlerr.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
    except Exception as e:                            # noqa: BLE001
        return {"__error__": "网络失败：%s" % e}
    try:
        return json.loads(raw)
    except Exception:                                 # noqa: BLE001
        return {"__error__": "返回非 JSON：%s" % raw[:400]}


def err_of(resp):
    if not isinstance(resp, dict):
        return "返回异常"
    return resp.get("__error__") or resp.get("Message") or resp.get("Code")


# ══════════════════════ 命令 ══════════════════════

def cmd_check(ak, args):
    r = call(ak, "DescribeRegions")
    e = err_of(r)
    if e:
        print("验证失败：%s" % e)
        return 1
    regions = [x["RegionId"] for x in r.get("Regions", {}).get("Region", [])]
    print("AK 有效 · 可见 %d 个地域" % len(regions))
    print("常用：%s" % ", ".join([x for x in ("cn-hangzhou", "cn-shanghai", "cn-shenzhen",
                                              "cn-beijing") if x in regions]))
    return 0


def cmd_types(ak, args):
    r = call(ak, "DescribeInstanceTypes", {"RegionId": args.region})
    e = err_of(r)
    if e:
        print("查询失败：%s" % e)
        return 1
    rows = r.get("InstanceTypes", {}).get("InstanceType", [])
    kw = (args.filter or "").lower()
    print("%-28s %6s %8s %10s" % ("规格", "vCPU", "内存GB", "家族"))
    for x in rows[:200]:
        name = x.get("InstanceTypeId", "")
        if kw and kw not in name.lower():
            continue
        print("%-28s %6s %8s %10s" % (name, x.get("CpuCoreCount"),
                                      x.get("MemorySize"), x.get("InstanceTypeFamily")))
    return 0


def cmd_images(ak, args):
    r = call(ak, "DescribeImages", {
        "RegionId": args.region, "OSType": "windows",
        "PageSize": 100, "ImageOwnerAlias": "system",
    })
    e = err_of(r)
    if e:
        print("查询失败：%s" % e)
        return 1
    imgs = r.get("Images", {}).get("Image", [])
    hit = [i for i in imgs if "2022" in (i.get("OSName", "") or "")
           or "2019" in (i.get("OSName", "") or "")]
    print("%-24s %-14s %s" % ("ImageId", "平台", "OSName"))
    for i in (hit or imgs)[:40]:
        print("%-24s %-14s %s" % (i.get("ImageId"), i.get("Platform"),
                                  i.get("OSName")))
    return 0


def cmd_price(ak, args):
    p = {"RegionId": args.region, "InstanceType": args.type,
         "InstanceChargeType": "PostPaid", "PriceUnit": "Hour"}
    if args.image:
        p["ImageId"] = args.image
    r = call(ak, "DescribePrice", p)
    e = err_of(r)
    if e:
        print("询价失败：%s" % e)
        print("（DescribePrice 对参数很敏感，缺 ImageId / 磁盘规格常报错，"
              "拿不准就直接在控制台看价格）")
        return 1
    print(json.dumps(r.get("PriceInfo", r), ensure_ascii=False, indent=2))
    return 0


def ensure_zone(ak, region):
    """没指定可用区就取第一个可用的，省得手工去查。"""
    r = call(ak, "DescribeZones", {"RegionId": region})
    if err_of(r):
        return ""
    zs = r.get("Zones", {}).get("Zone", [])
    for z in zs:
        if z.get("ZoneId"):
            return z["ZoneId"]
    return ""


def ensure_image(ak, region):
    """没指定镜像就挑 Windows Server 2022 中文版公共镜像。"""
    r = call(ak, "DescribeImages", {
        "RegionId": region, "OSType": "windows",
        "ImageOwnerAlias": "system", "PageSize": 100})
    if err_of(r):
        return "", ""
    imgs = r.get("Images", {}).get("Image", [])
    if not imgs:
        return "", ""
    def score(i):
        n = (i.get("OSName", "") or "") + (i.get("ImageName", "") or "")
        s = 0
        if "2022" in n: s += 4
        if "2019" in n: s += 2
        if "中文" in n or "Chinese" in n or "zh" in n.lower(): s += 3
        if "数据中心" in n or "Datacenter" in n or "dtc" in n.lower(): s += 2
        return s
    best = max(imgs, key=score)
    return best.get("ImageId", ""), best.get("OSName", "")


def pick_type(ak, region, min_cpu=4, min_mem=8):
    """不凭记忆猜规格名 —— 从 API 里挑满足条件且最小的（优先通用算力型 u1）。"""
    r = call(ak, "DescribeInstanceTypes", {"RegionId": region})
    if err_of(r):
        return ""
    rows = r.get("InstanceTypes", {}).get("InstanceType", [])
    cand = []
    for x in rows:
        try:
            c, m = int(x.get("CpuCoreCount", 0)), int(x.get("MemorySize", 0))
        except (TypeError, ValueError):
            continue
        if c >= min_cpu and m >= min_mem:
            cand.append((c, m, x.get("InstanceTypeId", "")))
    if not cand:
        return ""
    cand.sort()
    pref = [t for t in cand if t[2].startswith("ecs.u1")] or \
           [t for t in cand if t[2].startswith("ecs.g7")] or cand
    return pref[0][2]


def cmd_create(ak, args):
    if not args.type:
        args.type = pick_type(ak, args.region)
        if args.type:
            print("  自动选规格：%s" % args.type)
        else:
            print("  没能自动挑出规格，请用 types 命令查后带 --type")
            return 1
    zone = args.zone
    if not zone:
        zone = ensure_zone(ak, args.region)
        if zone:
            print("  自动选可用区：%s" % zone)
    image, osname = args.image, ""
    if not image:
        image, osname = ensure_image(ak, args.region)
        if image:
            print("  自动选镜像：%s（%s）" % (image, osname))
        else:
            print("  没能自动挑出镜像，请用 images 命令查后带 --image")
            return 1
    if not zone:
        print("  没能自动挑出可用区，请带 --zone")
        return 1
    if not args.password:
        print("  缺 --password（Windows 要求 8-30 位且含大小写字母与数字）")
        return 1

    plan = {
        "地域": args.region, "可用区": zone, "规格": args.type,
        "镜像": "%s %s" % (image, osname), "实例名": args.name,
        "系统盘": "%s %sG" % (args.disk_cat, args.disk_size),
        "带宽": "%s Mbps (%s)" % (args.bw, args.bw_charge),
        "付费": args.charge, "3389 来源": (args.allow_ip + "/32") if args.allow_ip else "0.0.0.0/0（全网！）",
    }
    print("准备创建（%s）：" % ("真实创建" if args.yes else "DRY-RUN，不花钱"))
    for k, v in plan.items():
        print("  %-10s %s" % (k, v))
    if not args.yes:
        print("\n加 --yes 才真的创建。建议先跑 price 看清价格，务必带 --allow-ip。")
        return 0

    # 1) VPC
    r = call(ak, "CreateVpc", {"RegionId": args.region, "CidrBlock": "172.16.0.0/16",
                               "VpcName": "quant-vpc"})
    if err_of(r):
        print("建 VPC 失败：%s" % err_of(r)); return 1
    vpc = r["VpcId"]
    print("  VPC %s" % vpc)
    time.sleep(3)

    # 2) VSwitch
    r = call(ak, "CreateVSwitch", {"RegionId": args.region, "VpcId": vpc,
                                   "ZoneId": zone, "CidrBlock": "172.16.1.0/24",
                                   "VSwitchName": "quant-vsw"})
    if err_of(r):
        print("建交换机失败：%s" % err_of(r)); return 1
    vsw = r["VSwitchId"]
    print("  VSwitch %s" % vsw)

    # 3) 安全组：只开 3389，且只放行指定来源 IP
    r = call(ak, "CreateSecurityGroup", {"RegionId": args.region, "VpcId": vpc,
                                         "SecurityGroupName": "quant-sg"})
    if err_of(r):
        print("建安全组失败：%s" % err_of(r)); return 1
    sg = r["SecurityGroupId"]
    cidr = (args.allow_ip + "/32") if args.allow_ip else "0.0.0.0/0"
    r = call(ak, "AuthorizeSecurityGroup", {
        "RegionId": args.region, "SecurityGroupId": sg,
        "IpProtocol": "TCP", "PortRange": "3389/3389", "SourceCidrIp": cidr,
        "Policy": "Accept", "Priority": 1})
    if err_of(r):
        print("放通 3389 失败：%s" % err_of(r)); return 1
    print("  安全组 %s · 3389 仅放行 %s" % (sg, cidr))
    if not args.allow_ip:
        print("  ⚠ 未按 IP 限制，3389 对全网开放 —— 强烈建议下次带 --allow-ip")

    # 4) 实例
    extra = {
        "RegionId": args.region, "InstanceType": args.type, "ImageId": image,
        "VSwitchId": vsw, "SecurityGroupId": sg, "InstanceName": args.name,
        "SystemDisk.Category": args.disk_cat, "SystemDisk.Size": str(args.disk_size),
        "InternetMaxBandwidthOut": str(args.bw), "InternetChargeType": args.bw_charge,
        "InstanceChargeType": args.charge, "Password": args.password,
        "Amount": "1", "ZoneId": zone,
    }
    r = call(ak, "RunInstances", extra)
    if err_of(r):
        print("创建实例失败：%s" % err_of(r)); return 1
    iid = r["InstanceIdSets"]["InstanceIdSet"][0]
    print("\n实例已创建：%s" % iid)
    print("等它变成 Running 再连（约 1-3 分钟）：")
    print("  python3 scripts/aliyun/provision.py status --region %s" % args.region)
    return 0


def cmd_status(ak, args):
    r = call(ak, "DescribeInstances", {"RegionId": args.region, "PageSize": 100})
    e = err_of(r)
    if e:
        print("查询失败：%s" % e); return 1
    rows = r.get("Instances", {}).get("Instance", [])
    if not rows:
        print("该地域没有实例"); return 0
    print("%-22s %-10s %-16s %-16s %s" % ("InstanceId", "状态", "公网IP", "私网IP", "规格"))
    for x in rows:
        ips = x.get("PublicIpAddress", {}).get("IpAddress") or []
        pri = (x.get("VpcAttributes", {}).get("PrivateIpAddress", {})
               .get("IpAddress") or [])
        print("%-22s %-10s %-16s %-16s %s" % (
            x.get("InstanceId"), x.get("Status"),
            ips[0] if ips else "-", pri[0] if pri else "-",
            x.get("InstanceType")))
    return 0


def _wait_invocation(ak, region, inv_id, timeout=180):
    """轮询云助手执行结果。"""
    end = time.time() + timeout
    while time.time() < end:
        r = call(ak, "DescribeInvocationResults", {
            "RegionId": region, "InvokeId": inv_id, "PageSize": 10})
        res = (r.get("Invocation", {}).get("InvocationResults", {})
                .get("InvocationResult") or [])
        if res:
            out = res[0].get("Output") or ""
            try:
                out = base64.b64decode(out).decode("utf-8", "ignore")
            except Exception:                         # noqa: BLE001
                pass
            return res[0].get("InvocationStatus"), out
        time.sleep(4)
    return "Timeout", ""


def send_cmd(ak, region, instance_id, ps, timeout=60, wait=True):
    r = call(ak, "SendCommand", {
        "RegionId": region, "InstanceId.1": instance_id,
        "CommandType": "RunPowerShellScript", "CommandContent": ps,
        "Timeout": str(timeout), "Name": "quant-deploy"})
    e = err_of(r)
    if e:
        return False, "下发失败：%s" % e
    inv = r.get("InvokeId")
    if not wait:
        return True, inv
    st, out = _wait_invocation(ak, region, inv, timeout=timeout + 120)
    return (st == "Success"), out


def cmd_cmd(ak, args):
    ok, out = send_cmd(ak, args.region, args.instance, args.script)
    print(out)
    return 0 if ok else 1


def cmd_deploy(ak, args):
    """把看门狗文件切成 base64 块送进实例 —— 全程不用远程桌面。"""
    files = ["watchdog.py", "calendar.json", "watchdog_config.example.json",
             "watchdog_config.json"]
    base = args.src
    blocks = []
    for f in files:
        p = os.path.join(base, f)
        if not os.path.exists(p):
            print("  跳过（本地没有）：%s" % f); continue
        raw = open(p, "rb").read()
        b64 = base64.b64encode(raw).decode()
        chunk = args.chunk
        n = max(1, (len(b64) + chunk - 1) // chunk)
        blocks.append((f, [b64[i * chunk:(i + 1) * chunk] for i in range(n)]))
    if not blocks:
        print("没有可部署的文件，检查 --src"); return 1

    print("准备送入 %d 个文件（%s）：" % (len(blocks), "真实执行" if args.yes else "DRY-RUN"))
    for f, cs in blocks:
        print("  %-32s %d 块" % (f, len(cs)))
    if not args.yes:
        print("\n加 --yes 才真的执行。"); return 0

    ok, out = send_cmd(ak, args.region, args.instance,
                       "New-Item -ItemType Directory -Force -Path C:\\quant\\in | Out-Null; "
                       "echo stagedir-ok")
    if not ok:
        print("建目录失败：%s" % out); return 1
    print("  目录就绪")

    for f, chunks in blocks:
        tmp = "C:\\quant\\in\\%s.b64" % f
        for i, c in enumerate(chunks):
            ps = "$b='%s'; [IO.File]::AppendAllText('%s',$b)" % (c, tmp)
            ok, out = send_cmd(ak, args.region, args.instance, ps)
            if not ok:
                print("  传 %s 第 %d 块失败：%s" % (f, i + 1, out)); return 1
        ps = ("$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
              "(Get-Content '%s' -Raw))); "
              "[IO.File]::WriteAllText('C:\\quant\\in\\%s',$d,"
              "[Text.Encoding]::UTF8); Remove-Item '%s' -Force; echo ok-%s"
              % (tmp, f, tmp, f))
        ok, out = send_cmd(ak, args.region, args.instance, ps)
        print("  %-32s %s" % (f, "完成" if ok else "失败：%s" % out))
        if not ok:
            return 1

    print("\n文件到位。接下来在实例里跑安装（需远程桌面或云助手下发 bootstrap）：")
    print("  python3 scripts/aliyun/provision.py cmd --region %s --instance %s "
          "--script \"python C:\\\\quant\\\\in\\\\watchdog.py --status\""
          % (args.region, args.instance))
    return 0


# ══════════════════════ CLI ══════════════════════

def main():
    ap = argparse.ArgumentParser(description="阿里云 ECS 交易主机开通与运维")
    ap.add_argument("cmd", choices=["check", "types", "images", "price",
                                    "create", "status", "cmd", "deploy"])
    ap.add_argument("--region", default="cn-hangzhou")
    ap.add_argument("--zone", default="")
    ap.add_argument("--type", default="", help="实例规格（留空则自动挑 ≥4C8G 的最小款）")
    ap.add_argument("--image", default="", help="镜像 ID（先跑 images 查）")
    ap.add_argument("--name", default="qmt-trade-01")
    ap.add_argument("--password", default="", help="Windows 管理员密码")
    ap.add_argument("--disk-cat", default="cloud_essd")
    ap.add_argument("--disk-size", type=int, default=100)
    ap.add_argument("--bw", type=int, default=5)
    ap.add_argument("--bw-charge", default="PayByTraffic")
    ap.add_argument("--charge", default="PostPaid")
    ap.add_argument("--allow-ip", default="", help="只允许该公网 IP 连 3389")
    ap.add_argument("--instance", default="", help="实例 ID（cmd/deploy 用）")
    ap.add_argument("--script", default="", help="PowerShell 脚本（cmd 用）")
    ap.add_argument("--src", default="qmt/watchdog", help="deploy 的文件来源目录")
    ap.add_argument("--chunk", type=int, default=12000, help="base64 分块大小")
    ap.add_argument("--filter", default="")
    ap.add_argument("--yes", action="store_true", help="真正执行（花钱/改机器）")
    args = ap.parse_args()

    ak_id = os.environ.get("ALIYUN_ACCESS_KEY_ID", "")
    ak_sec = os.environ.get("ALIYUN_ACCESS_KEY_SECRET", "")
    if not (ak_id and ak_sec):
        print("请先设置环境变量（不要写进任何文件）：")
        print("  export ALIYUN_ACCESS_KEY_ID=...")
        print("  export ALIYUN_ACCESS_KEY_SECRET=...")
        return 2
    ak = {"id": ak_id, "secret": ak_sec}

    fn = {"check": cmd_check, "types": cmd_types, "images": cmd_images,
          "price": cmd_price, "create": cmd_create, "status": cmd_status,
          "cmd": cmd_cmd, "deploy": cmd_deploy}[args.cmd]
    return fn(ak, args)


if __name__ == "__main__":
    sys.exit(main())
