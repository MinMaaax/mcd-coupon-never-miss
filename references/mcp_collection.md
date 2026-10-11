# 从 MCP 收集券数据

本文件说明怎么把 `mcd-mcp` 的返回**转成脚本能吃的 coupon JSON**。
这是人和脚本之间的唯一接口，格式错了后面全错。

---

## 一、运行前提

需要已配置 `mcd-mcp` 连接器。配置方式见项目根目录的 `mcp-config.example.json`，核心是：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCD_MCP_TOKEN}" }
    }
  }
}
```

**Token 绝不写进本 skill 的任何文件、任何对话记录。** 一律用环境变量注入。

> 建议先跑一次无网络的自测确认脚本没问题：
> `python3 scripts/mcd_coupon_keeper.py selftest`

---

## 二、采集时序（固定四步）

| # | 工具 | 参数 | 干什么 |
|---|---|---|---|
| 1 | `now-time-info` | 无 | 取服务器当前日期，**不要用本机日期猜** |
| 2 | `available-coupons` | 无 | 看有哪些券可领（只有标题+状态） |
| 3 | `auto-bind-coupons` | 无 | 一键领全部。**用户未明确要求时先问一句** |
| 4 | `query-my-coupons` | `page=1, pageSize=200` | 拉完整券包，这是唯一数据源 |

若券数超过 200，继续翻页 `page=2..5` 并合并。

> 为什么必须先领再查？
> 因为 `available-coupons` 不返回有效期和价格。**没领的券等于看不见的券。**

---

## 三、字段映射表

把 `query-my-coupons` 的每条记录按下面的规则落成 JSON 字段：

| 券 JSON 字段 | 从哪来 | 备注 |
|---|---|---|
| `name` | 券标题 | 原样复制 |
| `validityText` | 「有效期」 | **原样复制，一个字都不要改**，解析器靠它 |
| `priceText` | 「优惠」 | 原样复制，保留 `¥` 符号和括号里的说明 |
| `receivedText` | 「领取时间」 | 如 `3日前收到`、`今日收到` |
| `tags` | 「标签」 | 拆成数组，如 `["到店专用","外送专用"]` |
| `imageUrl` | 图片 `src` | 参与指纹计算，尽量保留完整 URL |
| `originalPrice` | 可选 | 从 `query-meals` 匹配到的门店实价，见下节 |

**不要做的事：**
- 不要把「优惠」里的 `¥4` 自作主张解读成「省 4 元」——语义不明，交给解析器判断
- 不要把有效期手工规整成统一格式——原样给解析器，它认得这四种形态
- 不要凭印象补 `validityText`——漏了字段这张券就没有效期

---

## 四、可选增强：注入真实原价

默认情况下「省了多少钱」是用 `data/tiering_config.json` 的**原价基线估算**的。
如果你想让结果更准，可以：

1. 调 `query-nearby-stores` 拿到 `storeCode`
2. 调 `query-meals` 拿门店菜单与价格
3. 把券名匹配到商品，取出实价填进 `originalPrice`

```json
{
  "name": "4折麦辣三件套券",
  "validityText": "...",
  "originalPrice": 39.0
}
```

填了就会走 `originalPriceSource = "meal-price"`，准确度最高。

**匹配不上就别硬填。** 留空会退回基线估算，结果差一点但不会错。
宁可标注「金额未知」，也不要给一个错误的数字。

---

## 五、落盘与运行

把整理好的 JSON 存成 `coupons.json`，然后：

```bash
# 一步跑完整条流水线
python3 scripts/mcd_coupon_keeper.py all --input coupons.json --outdir out/

# 指定今天（离线复现/测试用）
python3 scripts/mcd_coupon_keeper.py all --input coupons.json --outdir out/ --today 2026-10-10
```

产出：

| 文件 | 用途 |
|---|---|
| `out/report.md` | **直接读给用户听的简报**（含总览、提醒、排序表） |
| `out/calendar.html` | 可视化「餐段 × 日期」日历，可发给用户看 |
| `out/calendar.ics` | 导入手机日历，S 级券自带 T-3 提醒 |
| `out/tiered.json` | 分级明细，供二次处理 |
| `out/alerts.json` | 新券与提醒清单 |

台账默认写在 `data/coupon_ledger.json`，**别删**——它是「同一张券只提醒一次」的依据。

---

## 六、常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| MCP 连接报错 | 未配置 `mcd-mcp` 或 Token 失效 | 检查连接器配置 |
| 券列表为空 | 未登录 / 未绑定账号 | 引导用户在麦当劳 App 侧登录 |
| `validityText` 解析出 `null` | 字段漏传或用了非原文字符串 | 回查映射表，原样复制 |
| 所有券都「金额未知」 | 券名没命中基线关键词 | 在 `tiering_config.json` 补条目 |
| 反复提醒同一张券 | 台账被删或路径变了 | 固定 `--ledger` 路径 |
| 429 限流 | 请求过密 | 退避重试；一天跑一次足够 |
