# wechat-data-loop · 公众号数据回收回路

> 发完文章，自动把每篇的阅读、分享、阅读时长拉回来，算分享率，告诉你**"哪类内容真正管用"**——而不是靠感觉选题。

这是我（[cyber bird](https://github.com/cyberbird2048)）给自己公众号搭的数据回路。起因很丢人：**我写了 18 篇文章，从没看过一篇的数据。** 搭完才发现，自己花最多心思写的"深度分析"，读者平均 26 秒就走；随手做的小工具，反而打开率最高。

如果你也在凭感觉写公众号，这套可以让你看见自己的直觉哪里不准。

---

## 它怎么工作

```
Chrome(9222, 已登录公众号) ──CDP──▶ fetch_stats.py
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
              内容分析列表        逐篇进详情页         算分享率、标注口径
           (标题/阅读/msgid)    (分享/留言/时长)      (近1月可信/老文章失真)
                                        │
                                        ▼
                              output/stats.json  ← 喂给选题决策
                              output/summary.md  ← 人读排行
```

跑一次约 1 分钟（10 篇文章）。配 launchd 每天自动跑，**你发文后就不用再管数据了**。

---

## 前置条件（先看好再动手）

1. **一个公众号**（订阅号也行，用网页后台的数据，不需要服务号 API 权限）
2. **Chrome 以调试端口启动**：
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9222 --user-data-dir=~/chrome-debug-profile
   ```
   （用独立的 user-data-dir，避免和日常 Chrome 冲突）
3. **在该 Chrome 里登录一次公众号后台** `mp.weixin.qq.com`（扫码）。登录态会保持一段时间。
4. **Python + websocket-client**（用 `uv` 最省事）

> ⚠️ 看到这里你应该明白：这套方案绕开了公众号 API 的权限墙，代价是**依赖一个开着的、登录着的 Chrome**。不是那种装完就忘的云端服务。适合个人号主，不适合大规模。

---

## 快速开始

```bash
git clone https://github.com/cyberbird2048/cyberbird-lab.git
cd cyberbird-lab/wechat-data-loop

# 先 dry-run 看一眼（不写文件）
uv run --with websocket-client python3 fetch_stats.py --dry

# 正式跑
uv run --with websocket-client python3 fetch_stats.py
# → 生成 output/stats.json + output/summary.md
```

---

## 输出说明

### `output/stats.json`
```json
{
  "recovered_at": "2026-08-08T20:01:16",
  "articles": [
    {
      "date": "2026-08-03",
      "title": "WorkBuddy 混元3限免怎么薅最值",
      "reads": 125,
      "shares": 11,
      "comments": 0,
      "duration_min": 0.88,
      "share_rate": 8.8,
      "sample": "ok"
    }
  ]
}
```

### 关键指标怎么读
| 指标 | 反映什么 |
|------|---------|
| 阅读量 | 标题钩子 + 选题热度 |
| **分享率**（分享÷阅读） | **社交货币**——读者愿不愿意用它代表自己。这是最该盯的 |
| 平均阅读时长 | 内容留人能力（太短=标题骗进来留不住） |

### ⚠️ 一个口径陷阱
`reads` 是公众号列表的"近 30 天阅读人数"。所以**最近 35 天内发的文章**（`sample: "ok"`）分享率可信；更早的文章（`sample: "old"`）分享率会失真（甚至 >100%，因为分享是累计而阅读是 30 天窗口）。**评估时只看 `sample: ok` 的那些。**

---

## 自动化（每天跑一次）

把 `launchd/com.cyberbird.wechat-stats-recover.plist` 复制到 `~/Library/LaunchAgents/`，**把里面的 `<YOUR_USERNAME>` 和路径替换成你自己的**，然后：

```bash
launchctl load -w ~/Library/LaunchAgents/com.cyberbird.wechat-stats-recover.plist
```

默认每天 11:00 跑。前提：那个调试 Chrome 一直开着且登录态有效。

---

## 诚实的限制

- **登录态会过期**：cookie 失效后脚本会检测到"未登录"并退出（不会崩，日志会记），你重新扫码登录即可恢复。
- **依赖浏览器进程**：Chrome 关了就跑不了。不是无头服务。
- **"在看"和"收藏"没抓**：它们在公众号后台的"互动管理"页，不在"内容分析"。本脚本只取内容分析的数据。够用，因为分享率才是关键。
- **DOM 可能变**：公众号后台改版会导致选择器失效。脚本里的提取逻辑写得比较鲁棒（按"标签+数字"模式找，不依赖死 class），但若失效，照着 `DETAIL_EXTRACT` 那段 JS 调一下即可。
- **公众号数据有 1-2 天延迟**：刚发的文章数据要等一两天才准。

---

## 为什么不用公众号 API？

个人订阅号没有 `freepublish/batchget` 等数据接口的权限（那是认证服务号的）。CDP 抓网页后台是个人号主唯一可行的路。代价就是上面那些限制。

---

## License

MIT，见[根目录 LICENSE](../LICENSE)。拿去用，注明来源即可。

做这个的过程中我学到一件事：**数据没说我的分析文写得差，它说的是——在我这个号上，这类内容读者不感兴趣。** 两件事别搞混。希望对你也有用。
