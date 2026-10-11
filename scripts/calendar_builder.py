#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""calendar_builder.py - 把分级后的券渲染成「餐段 x 日期」使用日历。

输出：
  calendar.html  自包含的可视化日历（无外部依赖，双击即看）
  calendar.ics   可导入手机系统日历，极品券自带 T-3 天提醒

用法：
    python3 calendar_builder.py --input tiered.json --outdir out/
    python3 calendar_builder.py --input tiered.json --outdir out/ --days 7
"""

import argparse
import html
import json
import os
import sys
from datetime import date, datetime, timedelta

try:
    from value_tier import MEAL_PERIODS, load_config, tier_payload
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from value_tier import MEAL_PERIODS, load_config, tier_payload

WEEKDAY_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

GRADE_STYLE = {
    'S': ('#D32F2F', '#FFFFFF'),
    'A': ('#FAC775', '#412402'),
    'B': ('#9FE1CB', '#04342C'),
    'C': ('#D3D1C7', '#2C2C2A'),
}
GRADE_NAME = {'S': '极品券', 'A': '精品券', 'B': '普通券', 'C': '渣渣券'}
URGENCY_MARK = {'critical': '特急', 'high': '临期', 'medium': '留意', 'normal': ''}

ICS_MAX_DAYS = 30  # 超过这个天数的券不写进 .ics，避免生成跨月长条事件


def _d(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M').date()


def coupon_active_on(coupon, day):
    vf, vt = coupon.get('validFrom'), coupon.get('validTo')
    if not vf or not vt:
        return True
    if not (_d(vf) <= day <= _d(vt)):
        return False
    wd = coupon.get('weekdays')
    if wd and day.weekday() not in wd:
        return False
    return True


def _short_label(coupon, limit=9):
    name = coupon.get('name') or ''
    name = name.replace('券', '').replace('™', '')
    if len(name) > limit:
        name = name[:limit]
    return name


def _cell_sort_key(c):
    """单元格与常驻区的排序键：等级优先，再按价值分、省下的钱降序。

    免费券的 valueScore 一律被拉平到 1.0，所以「省得多」的次键
    决定 0 元月堡排在 0 元大米派之前。
    """
    saving = c.get('saving')
    return (['S', 'A', 'B', 'C'].index(c.get('grade', 'C')),
            -(c.get('valueScore') or 0),
            -(float(saving) if saving is not None else -1.0))


def split_resident(tiered, day_list):
    """窗口内每天都可用的券归入「常驻券」，其余进网格。"""
    resident, moving = [], []
    for c in tiered:
        covered = sum(1 for d in day_list if coupon_active_on(c, d))
        (resident if covered == len(day_list) else moving).append(c)
    return resident, moving


def _consecutive_runs(indices):
    """把升序下标列表切成若干连续区间 [(start, end), ...]。"""
    runs, cur = [], []
    for i in indices:
        if cur and i == cur[-1] + 1:
            cur.append(i)
        else:
            if cur:
                runs.append((cur[0], cur[-1]))
            cur = [i]
    if cur:
        runs.append((cur[0], cur[-1]))
    return runs


def build_lanes(tiered, day_list):
    """把券排进「餐段 x 泳道」结构。

    同一张券在连续多天可用时合并成**一条横条**（跨列表格单元格），
    而不是每天重复一个小券块 —— 后者容易被误读成「每天各有一张券」。

    返回 lanes[餐段] = [泳道, ...]，每条泳道是 {起始列: (结束列, 券)}，
    同一泳道内的长条互不重叠。
    """
    period_names = [p[0] for p in MEAL_PERIODS]
    runs = {p: [] for p in period_names}

    # 1) 按餐段收集所有「连续可用段」
    for c in tiered:
        days = [i for i, d in enumerate(day_list) if coupon_active_on(c, d)]
        if not days:
            continue
        segs = _consecutive_runs(days)
        for pname in c.get('periods') or []:
            if pname not in runs:
                continue
            for s, e in segs:
                runs[pname].append((s, e, c))

    # 2) 贪心排泳道：价值高的券先占靠上的泳道
    lanes = {}
    for pname in period_names:
        items = sorted(runs[pname], key=lambda r: (_cell_sort_key(r[2]), r[0], -(r[1] - r[0])))
        placed = []
        for s, e, c in items:
            for lane in placed:
                if all(e2 < s or s2 > e for s2, (e2, _) in lane.items()):
                    lane[s] = (e, c)
                    break
            else:
                placed.append({s: (e, c)})
        lanes[pname] = placed
    return lanes


def render_html(tiered_payload, days=7, start=None):
    coupons = tiered_payload.get('coupons') or []
    day_list = [(start or date.today()) + timedelta(days=i) for i in range(days)]
    resident, moving = split_resident(coupons, day_list)
    lanes = build_lanes(moving, day_list)
    summary = tiered_payload.get('gradeSummary') or {}
    alerts = [c for c in coupons if c.get('grade') == 'S' and c.get('urgency') in ('critical', 'high')]

    def chip(c, span=1, span_range=None):
        """券块。span > 1 时是一条横跨多天的长条。"""
        grade = c.get('grade', 'C')
        bg, fg = GRADE_STYLE.get(grade, GRADE_STYLE['C'])
        label = html.escape(_short_label(c))
        if c.get('isFree'):
            label = '<i class="free-badge">0元</i>' + label
        span_note = ''
        if span > 1:
            label = '<span class="chip-label">%s</span>' % label
            span_note = '<i class="span-days">连用 %d 天</i>' % span
        valid_note = c.get('validTo')
        if span_range:
            valid_note = '本段 %s 起连续 %d 天' % (span_range[0], span)
        title = html.escape('%s｜%s｜%s%s' % (
            c.get('name'), c.get('gradeLabel'), valid_note,
            ('｜省 ¥%.1f' % c['saving']) if c.get('saving') is not None else '｜金额未知'))
        cls = 'chip'
        if span > 1:
            cls += ' chip-span'
        if grade == 'S':
            cls += ' chip-s'
        if c.get('isFree'):
            cls += ' chip-free'
        return ('<span class="%s" style="background:%s;color:%s" title="%s">%s%s</span>'
                % (cls, bg, fg, title, label, span_note))

    head_cells = ['<th class="rh">餐段</th>']
    for i, d in enumerate(day_list):
        cls = ' class="today"' if i == 0 else ''
        head_cells.append('<th%s>%d/%d<br>%s</th>' % (cls, d.month, d.day, WEEKDAY_CN[d.weekday()]))

    rows = []
    for pname, pstart, pend in MEAL_PERIODS:
        lane_list = lanes.get(pname) or []
        if not lane_list:
            cells = ['<td class="rh">%s</td>' % pname]
            cells += ['<td><span class="none">-</span></td>' for _ in day_list]
            rows.append('<tr>%s</tr>' % ''.join(cells))
            continue
        for li, lane in enumerate(lane_list):
            cells = []
            if li == 0:
                cells.append('<td class="rh" rowspan="%d">%s</td>' % (len(lane_list), pname))
            i = 0
            while i < len(day_list):
                if i in lane:
                    e, c = lane[i]
                    span = e - i + 1
                    a, b = day_list[i], day_list[e]
                    rng = ('%d/%d' % (a.month, a.day), '%d/%d' % (b.month, b.day))
                    cells.append('<td class="span-cell" colspan="%d">%s</td>'
                                 % (span, chip(c, span=span, span_range=rng if span > 1 else None)))
                    i = e + 1
                else:
                    cells.append('<td class="blank"></td>')
                    i += 1
            rows.append('<tr%s>%s</tr>'
                        % (' class="lane"' if li < len(lane_list) - 1 else '', ''.join(cells)))

    resident_html = ''
    if resident:
        chips = ''.join(chip(c) for c in sorted(resident, key=_cell_sort_key))
        resident_html = (
            '<div class="resident"><span class="resident-label">常驻券（%d 天内每天可用）</span>%s</div>'
            % (days, chips))

    alert_html = ''
    for c in alerts:
        alert_html += ('<div class="alert">S 级「%s」%s 到期，剩余 %s 天 —— 已进入提醒窗口</div>'
                       % (html.escape(c.get('name') or ''),
                          html.escape((c.get('validTo') or '')[:10]),
                          c.get('daysLeft')))

    legend = ''.join(
        '<span class="lg"><i style="background:%s"></i>%s %s</span>'
        % (GRADE_STYLE[g][0], g, GRADE_NAME[g]) for g in ['S', 'A', 'B', 'C'])
    legend += '<span class="lg"><i class="free-badge">0元</i>免费券（一律判为极品券）</span>'

    period_note = ' ｜ '.join('%s %s-%s' % p for p in MEAL_PERIODS)

    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦麦省心 · 券使用日历</title>
<style>
:root { color-scheme: light; }
body { margin:0; padding:24px; background:#F7F6F3; color:#2C2C2A;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; font-size:13px; }
.wrap { max-width:980px; margin:0 auto; }
h1 { font-size:16px; font-weight:500; margin:0 0 4px; }
.sub { font-size:12px; color:#5F5E5A; margin-bottom:14px; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }
.card { background:#fff; border:0.5px solid rgba(0,0,0,.12); border-radius:10px; padding:10px 14px; min-width:88px; }
.card .k { font-size:12px; color:#5F5E5A; }
.card .v { font-size:20px; font-weight:500; margin-top:2px; }
.legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:#5F5E5A; margin-bottom:10px; }
.lg { display:flex; align-items:center; gap:5px; }
.lg i { width:10px; height:10px; border-radius:2px; display:inline-block; }
.panel { background:#fff; border:0.5px solid rgba(0,0,0,.12); border-radius:14px; overflow:hidden; }
table { width:100%; border-collapse:collapse; table-layout:fixed; font-size:11px; }
th { padding:6px 3px; font-weight:500; font-size:11px; color:#5F5E5A; text-align:center;
  background:#FAFAF8; border-bottom:0.5px solid rgba(0,0,0,.10); }
th.rh { text-align:left; width:64px; }
th.today { color:#791F1F; background:#FCEBEB; }
td { padding:5px 3px; vertical-align:top; border-bottom:0.5px solid rgba(0,0,0,.07); }
td.rh { font-size:11px; color:#5F5E5A; }
.chip { display:block; padding:2px 4px; margin-bottom:2px; border-radius:4px; font-size:11px;
  line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; cursor:default; }
.chip-s { font-weight:500; letter-spacing:.2px; }
/* 跨天长条：同一张券连续多天可用时合并为一条横向长条，不再逐格重复 */
.chip-span { display:flex; align-items:center; gap:5px; }
.chip-span .chip-label { flex:1 1 auto; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chip-span .span-days { flex:none; font-style:normal; font-size:10px; opacity:.8; }
.chip-span .free-badge { flex:none; }
td.blank { border-bottom:0.5px solid rgba(0,0,0,.07); }
tr.lane td { border-bottom:0.5px dashed rgba(0,0,0,.05); }
.chip-free { box-shadow: inset 0 0 0 1px rgba(255,255,255,.55); font-weight:500; }
.free-badge { display:inline-block; font-style:normal; font-size:10px; line-height:1.4;
  background:rgba(74,27,12,.14); border-radius:3px; padding:0 3px; margin-right:3px; white-space:nowrap; }
.chip .free-badge { background:#FFFFFF; color:#791F1F; font-weight:500; }
.none { color:#B4B2A9; }
.resident { padding:10px 12px; border-top:0.5px solid rgba(0,0,0,.10); }
.resident-label { font-size:11px; color:#5F5E5A; margin-right:8px; }
.resident .chip { display:inline-block; margin:0 4px 4px 0; }
.alert { margin-top:10px; padding:8px 12px; border-radius:8px; background:#FCEBEB; color:#791F1F;
  border-left:3px solid #D32F2F; font-size:12px; line-height:1.6; }
.note { margin-top:10px; font-size:11px; color:#888780; line-height:1.7; }
</style>
</head>
<body>
<div class="wrap">
  <h1>麦麦省心 · 券使用日历</h1>
  <div class="sub">生成时间 __GENERATED__ ｜ 共 __COUNT__ 张有效券</div>
  <div class="cards">
    <div class="card"><div class="k">极品券 S</div><div class="v" style="color:__S_COLOR__">__S__</div></div>
    <div class="card"><div class="k">精品券 A</div><div class="v">__A__</div></div>
    <div class="card"><div class="k">普通券 B</div><div class="v">__B__</div></div>
    <div class="card"><div class="k">渣渣券 C</div><div class="v">__C__</div></div>
  </div>
  <div class="legend">__LEGEND__</div>
  <div class="panel">
    <table>
      <thead><tr>__HEAD__</tr></thead>
      <tbody>__ROWS__</tbody>
    </table>
    __RESIDENT__
  </div>
  __ALERTS__
  <div class="note">
    餐段划分：__PERIODS__<br>
    <b>同一条横条代表同一张券连续多天可用</b>——横跨几格就是「连用几天」，不是每天各有一张券；空白格表示该餐段当天没有券。<br>
    0 元券（券面写明「用券价格 ¥0」，或券名含「免费 / 免单 / 0 元」）一律判为极品券，不参与加权打分。<br>
    原价与节省金额为估算值（来源见 references/tiering_rules.md），实际以下单页面的 calculate-price 结果为准。<br>
    本页仅用于券的资产管理，不构成下单建议；本工具不会自动下单。
  </div>
</div>
</body>
</html>
"""\
        .replace('__GENERATED__', html.escape(tiered_payload.get('generatedAt') or
                                             datetime.now().strftime('%Y-%m-%d %H:%M'))) \
        .replace('__COUNT__', str(len(coupons))) \
        .replace('__S_COLOR__', GRADE_STYLE['S'][0]) \
        .replace('__S__', str(summary.get('S', 0))) \
        .replace('__A__', str(summary.get('A', 0))) \
        .replace('__B__', str(summary.get('B', 0))) \
        .replace('__C__', str(summary.get('C', 0))) \
        .replace('__LEGEND__', legend) \
        .replace('__HEAD__', ''.join(head_cells)) \
        .replace('__ROWS__', ''.join(rows)) \
        .replace('__RESIDENT__', resident_html) \
        .replace('__ALERTS__', alert_html) \
        .replace('__PERIODS__', period_note)


# ---------------------------------------------------------------- ICS

def _ics_escape(s):
    return (s or '').replace('\\', '\\\\').replace(';', '\\;')\
        .replace(',', '\\,').replace('\r\n', '\\n').replace('\n', '\\n')


def build_ics(tiered_payload, today=None, max_days=ICS_MAX_DAYS):
    today = today or date.today()
    coupons = tiered_payload.get('coupons') or []
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S')

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//mcd-coupon-keeper//CN',
        'CALSCALE:GREGORIAN',
        'X-WR-CALNAME:麦当劳优惠券',
    ]
    for c in coupons:
        vf, vt = c.get('validFrom'), c.get('validTo')
        if not vf or not vt:
            continue
        d0, d1 = _d(vf), _d(vt)
        if (d1 - today).days > max_days:
            continue
        if d0 < today:
            d0 = today
        if d1 < d0:
            continue

        desc_bits = ['等级：%s' % c.get('gradeLabel', '')]
        if c.get('saving') is not None:
            desc_bits.append('预计省 ¥%.2f（原价估算 ¥%.2f）' % (c['saving'], c['originalPrice']))
        else:
            desc_bits.append('节省金额未知')
        if c.get('periods'):
            desc_bits.append('可用餐段：%s' % '、'.join(c['periods']))
        desc_bits.append('渠道：%s' % ('到店+外送' if len(set(c.get('channels') or [])) >= 2 else '单一渠道'))
        if c.get('rawValidity'):
            desc_bits.append('有效期原文：%s' % c['rawValidity'])

        ev = [
            'BEGIN:VEVENT',
            'UID:%s@mcd-coupon-keeper' % c.get('fingerprint', ''),
            'DTSTAMP:%sZ' % stamp,
            'DTSTART;VALUE=DATE:%s' % d0.strftime('%Y%m%d'),
            'DTEND;VALUE=DATE:%s' % (d1 + timedelta(days=1)).strftime('%Y%m%d'),
            'SUMMARY:%s' % _ics_escape('【%s】%s' % (c.get('grade', 'C'), c.get('name', ''))),
            'DESCRIPTION:%s' % _ics_escape('\n'.join(desc_bits)),
        ]
        if c.get('grade') == 'S':
            ev += [
                'BEGIN:VALARM',
                'ACTION:DISPLAY',
                'TRIGGER:-P3D',
                'DESCRIPTION:%s' % _ics_escape('极品券 3 天后到期：%s' % c.get('name', '')),
                'END:VALARM',
            ]
        ev.append('END:VEVENT')
        lines.extend(ev)

    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description='麦当劳券使用日历生成器')
    ap.add_argument('--input', '-i', required=True, help='分级后的 JSON（value_tier 的输出）')
    ap.add_argument('--outdir', '-o', default='.', help='输出目录，默认当前目录')
    ap.add_argument('--days', type=int, default=7, help='日历展示天数，默认 7')
    ap.add_argument('--today', help='覆盖今天日期 YYYY-MM-DD')
    ap.add_argument('--no-ics', action='store_true', help='不生成 .ics')
    args = ap.parse_args(argv)

    with open(args.input, 'r', encoding='utf-8') as f:
        payload = json.load(f)

    today = datetime.strptime(args.today, '%Y-%m-%d').date() if args.today else None
    os.makedirs(args.outdir, exist_ok=True)

    payload = dict(payload)
    payload['generatedAt'] = datetime.now().strftime('%Y-%m-%d %H:%M')

    html_path = os.path.join(args.outdir, 'calendar.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(render_html(payload, days=args.days, start=today))
    print('已生成日历 %s' % html_path)

    if not args.no_ics:
        ics_path = os.path.join(args.outdir, 'calendar.ics')
        with open(ics_path, 'w', encoding='utf-8') as f:
            f.write(build_ics(payload, today=today))
        print('已生成日历文件 %s' % ics_path)


if __name__ == '__main__':
    main()
