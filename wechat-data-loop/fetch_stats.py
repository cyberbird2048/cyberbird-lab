#!/usr/bin/env python3
"""
公众号数据回收回路 · wechat-data-loop
通过 Chrome DevTools Protocol 自动抓取公众号后台"内容分析"，回收每篇文章的
阅读/分享/留言/阅读时长，算分享率，输出 stats.json + summary.md。

前置：
  1. Chrome 以 --remote-debugging-port=9222 启动
  2. 在该 Chrome 里登录公众号后台（mp.weixin.qq.com）一次
  3. websocket-client 可用

用法：
  uv run --with websocket-client python3 fetch_stats.py             # 抓取+写 output/
  uv run --with websocket-client python3 fetch_stats.py --dry       # 只打印不写
  uv run --with websocket-client python3 fetch_stats.py --no-detail # 只抓列表（快）
  uv run --with websocket-client python3 fetch_stats.py --output ~/my-stats

输出（默认 ./output/）：
  - stats.json   结构化数据（机器读，喂给评估）
  - summary.md   人读摘要 + 分享率排行
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, date
from pathlib import Path

try:
    import websocket
except ImportError:
    print("缺 websocket-client。用：uv run --with websocket-client python3 fetch_stats.py", file=sys.stderr)
    sys.exit(1)

CDP_HTTP = "http://localhost:9222"
REPORT_PATH = "/misc/appmsganalysis?action=report&type=daily_v2&lang=zh_CN"


# ---------- CDP 基础 ----------
def cdp_tabs():
    with urllib.request.urlopen(f"{CDP_HTTP}/json", timeout=6) as r:
        return json.loads(r.read())


def find_weixin_tab():
    pages = [t for t in cdp_tabs() if t.get("type") == "page"]
    wt = [t for t in pages if "mp.weixin" in t.get("url", "")]
    if wt:
        return wt[0]
    req = urllib.request.Request(f"{CDP_HTTP}/json/new?https://mp.weixin.qq.com/", method="PUT")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=30)
        self._i = 0

    def eval(self, expr):
        self._i += 1
        i = self._i
        self.ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True, "awaitPromise": False}}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == i:
                break
        v = r.get("result", {}).get("result", {}).get("value")
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return v
        return v

    def nav(self, url, wait=6):
        self.ws.send(json.dumps({"id": 99000, "method": "Page.navigate", "params": {"url": url}}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == 99000:
                break
        time.sleep(wait)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def get_token(cdp):
    href = cdp.eval("location.href") or ""
    m = re.search(r"token=(\d+)", href)
    return m.group(1) if m else None


# ---------- 抓取 ----------
def grab_list(cdp, token):
    """内容分析列表页：从'详情'a 提取 [{msgid,date,title,read}]。"""
    cdp.nav(f"https://mp.weixin.qq.com{REPORT_PATH}&token={token}", wait=7)
    lst = cdp.eval(r"""(function(){
      var out=[];
      document.querySelectorAll('a').forEach(function(a){
        var h=a.getAttribute('href')||'';
        var m=h.match(/msgid=([^&]+).*publish_date=([\d-]+)/);
        if(m){
          var tbl=a.closest('table'); var tds=tbl?tbl.querySelectorAll('td'):[];
          var title='',read='';
          tds.forEach(function(td){var txt=(td.innerText||'').trim().replace(/\s+/g,' ');
            if(txt&&!/详情|近30天|趋势/.test(txt)){if(!title)title=txt;
            else if(/^[\d,]+$/.test(txt)&&!read)read=txt}});
          out.push({msgid:m[1],date:m[2],title:title.slice(0,40),read:read});
        }
      });
      var seen={},res=[];
      out.forEach(function(x){if(!seen[x.msgid]){seen[x.msgid]=1;res.push(x)}});
      return JSON.stringify(res);
    })()""")
    return lst if isinstance(lst, list) else []


# 详情页：叶子=标签 → 父块 → 正则取数
DETAIL_EXTRACT = r"""(function(){
  var res={};
  document.querySelectorAll('*').forEach(function(e){
    if(e.children.length===0){
      var lt=(e.innerText||'').trim();
      if(/^(阅读人数|阅读次数|阅读|在看|分享|收藏|留言|点赞|平均阅读时长)$/.test(lt)){
        var p=e.parentElement;
        var blk=(p?p.innerText:'').replace(/[\s\n]+/g,' ').trim().slice(0,30);
        if(/\d/.test(blk)&&!res[lt]){res[lt]=blk}
      }
    }
  });
  return JSON.stringify(res);
})()"""


def parse_detail(d):
    r = {}
    M = {
        '阅读人数': (r'阅读人数\s*([\d,]+)', 'reads_d'),
        '阅读次数': (r'阅读次数\s*([\d,]+)', 'reads_cnt'),
        '阅读': (r'阅读\s*([\d,]+)', 'reads_d3'),
        '在看': (r'在看\s*([\d,]+)', 'wow'),
        '分享': (r'分享\s*([\d,]+)', 'shares'),
        '收藏': (r'收藏\s*([\d,]+)', 'favs'),
        '留言': (r'留言\s*([\d,]+)', 'comments'),
        '平均阅读时长': (r'([\d.]+)\s*分钟', 'duration_min'),
    }
    for k, v in (d or {}).items():
        if k in M:
            mm = re.search(M[k][0], v)
            if mm:
                r[M[k][1]] = mm.group(1).replace(',', '')
    return r


def grab_detail(cdp, token, msgid, d):
    cdp.nav(f"https://mp.weixin.qq.com/misc/appmsganalysis?action=detailpage&msgid={msgid}"
            f"&publish_date={d}&type=int&token={token}&lang=zh_CN", wait=5)
    return parse_detail(cdp.eval(DETAIL_EXTRACT))


def annotate(rows):
    """算分享率，标注口径可信度（近35天=ok，老文章=old，因阅读数是近30天窗口会失真）。"""
    today = date.today()
    for a in rows:
        try:
            dd = datetime.strptime(a["date"], "%Y-%m-%d").date()
            a["sample"] = "ok" if (today - dd).days <= 35 else "old"
        except Exception:
            a["sample"] = "?"
        try:
            r = int(a.get("reads") or 0)
        except (ValueError, TypeError):
            r = 0
        sh = a.get("shares")
        a["share_rate"] = round(int(sh) / r * 100, 1) if (str(sh).isdigit() and r) else None
    return rows


# ---------- 输出 ----------
def write_outputs(rows, outdir, dry):
    outdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "recovered_at": datetime.now().isoformat(timespec="seconds"),
        "source": "公众号后台 内容分析 (CDP)",
        "note": "reads 为近30天窗口口径；老文章 share_rate 失真(>100%)；在看/收藏未取(在互动管理页)",
        "count": len(rows),
        "articles": rows,
    }
    if dry:
        print("\n[dry] 预览：")
        for a in rows:
            print(f"  {a['date']} | 阅{a.get('reads','?'):>4} 分{a.get('shares','?'):>3}"
                  f"({a.get('share_rate','?')}%) 留{a.get('comments','?')} 时{a.get('duration_min','?')} | {a['title'][:20]}")
        return
    (outdir / "stats.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 数据回收摘要 · {payload['recovered_at'][:10]}\n",
             "| 日期 | 阅读 | 分享 | 分享率 | 留言 | 时长(min) | 标题 |",
             "|------|------|------|--------|------|-----------|------|"]
    for a in sorted(rows, key=lambda x: int(x.get("reads") or 0), reverse=True):
        lines.append(f"| {a['date']} | {a.get('reads','?')} | {a.get('shares','?')} | "
                     f"{a.get('share_rate','?')}% | {a.get('comments','?')} | {a.get('duration_min','?')} | {a['title'][:24]} |")
    ok = [a for a in rows if a.get("sample") == "ok" and a.get("share_rate") is not None]
    if ok:
        best = max(ok, key=lambda x: x["share_rate"])
        lines.append(f"\n**[近1月分享率最高]** {best['title'][:20]} — {best['share_rate']}%")
    (outdir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"✓ 写 {outdir/'stats.json'} + {outdir/'summary.md'}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="公众号数据回收回路")
    ap.add_argument("--output", default="./output", help="输出目录（默认 ./output）")
    ap.add_argument("--dry", action="store_true", help="只打印不写")
    ap.add_argument("--no-detail", action="store_true", help="只抓列表，不进详情")
    args = ap.parse_args()
    outdir = Path(args.output).resolve()

    cdp = CDP(find_weixin_tab()["webSocketDebuggerUrl"])
    token = get_token(cdp)
    if not token:
        cdp.nav("https://mp.weixin.qq.com/", wait=6)
        token = get_token(cdp)
    if not token:
        print("❌ 未登录。请在 Chrome(9222) 登录公众号后台后重跑。", file=sys.stderr)
        cdp.close()
        sys.exit(2)
    print(f"✓ 已登录 token={token}", file=sys.stderr)

    rows = grab_list(cdp, token)
    if not rows:
        print("⚠️ 列表为空（页面未加载完或 DOM 变了）", file=sys.stderr)
        cdp.close()
        sys.exit(3)
    print(f"✓ 列表 {len(rows)} 篇", file=sys.stderr)

    if not args.no_detail:
        for a in rows:
            try:
                det = grab_detail(cdp, token, a["msgid"], a["date"])
                a.update({k: v for k, v in det.items() if v})
            except Exception as e:
                print(f"  详情失败 {a['title'][:14]}: {repr(e)[:50]}", file=sys.stderr)
            a["reads"] = (a.get("reads_d") or a.get("reads_d3") or a.get("reads_cnt")
                          or a.get("read") or a.get("reads"))

    rows = annotate(rows)
    write_outputs(rows, outdir, args.dry)
    cdp.close()
    print("✓ 完成", file=sys.stderr)


if __name__ == "__main__":
    main()
