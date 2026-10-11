#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcd_coupon_keeper.py - 麦麦省心统一入口。

把「解析 -> 分级 -> 台账提醒 -> 生成日历 -> 生成简报」串成一条流水线。

用法：
    # 一步跑完整条流水线
    python3 mcd_coupon_keeper.py all --input coupons.json --outdir out/

    # 分步
    python3 mcd_coupon_keeper.py parse    --input coupons.json
    python3 mcd_coupon_keeper.py tier     --input coupons.json --out tiered.json
    python3 mcd_coupon_keeper.py alert    --input tiered.json
    python3 mcd_coupon_keeper.py calendar --input tiered.json --outdir out/

    # 用内置样例数据自测（不需要 MCP）
    python3 mcd_coupon_keeper.py selftest
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from coupon_parser import parse_payload                      # noqa: E402
from value_tier import load_config, tier_payload, sort_key   # noqa: E402
from calendar_builder import render_html, build_ics          # noqa: E402
from alert_checker import load_ledger, save_ledger, check    # noqa: E402

SAMPLE = os.path.join(SKILL_ROOT, 'data', 'sample_coupons.json')
DEFAULT_LEDGER = os.path.join(SKILL_ROOT, 'data', 'coupon_ledger.json')

URGENCY_ORDER = {'critical': '特急', 'high': '临期', 'medium': '留意', 'normal': '常规'}


def read_payload(path):
    if path:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return json.load(sys.stdin)


def write_text(path, text):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def resolve_today(s):
    return datetime.strptime(s, '%Y-%m-%d').date() if s else date.today()


def resolve_now(s, today):
    if not s:
        return None
    h, m = s.split(':')
    return datetime(today.year, today.month, today.day, int(h), int(m))


# ---------------------------------------------------------------- 简报

def build_report(tiered, alerts, today):
    coupons = tiered.get('coupons') or []
    s = tiered.get('gradeSummary') or {}
    L = []
    L.append('# 麦麦省心 · %s 简报' % today.isoformat())
    L.append('')
    L.append('## 总览')
    L.append('- 券包共 **%d** 张有效券（本次新领 %d 张，去重合并 %d 张）'
             % (tiered.get('count', 0), alerts.get('newCount', 0),
                tiered.get('duplicatesRemoved', 0)))
    L.append('- 极品券 **%d** 张 ｜ 精品券 %d 张 ｜ 普通券 %d 张 ｜ 渣渣券 %d 张'
             % (s.get('S', 0), s.get('A', 0), s.get('B', 0), s.get('C', 0)))
    free_coupons = [c for c in coupons if c.get('isFree')]
    if free_coupons:
        L.append('- 其中 **0 元免费券 %d 张**（免费一律判为极品券，不参与加权打分）'
                 % len(free_coupons))
    L.append('')

    if free_coupons:
        L.append('## 0 元免费券（%d 张）' % len(free_coupons))
        for c in sorted(free_coupons, key=lambda x: -(x.get('saving') or 0)):
            sv = ('省 ¥%.1f' % c['saving']) if c.get('saving') is not None else '原价未知'
            L.append('- **%s** ｜ %s ｜ %s 到期 ｜ 可用：%s'
                     % (c.get('name'), sv, (c.get('validTo') or '')[:10] or '长期有效',
                        '、'.join(c.get('periods') or []) or '全天'))
        L.append('')

    if alerts.get('alerts'):
        L.append('## 极品券提醒（过期前 3 天）')
        for a in alerts['alerts']:
            L.append('- %s' % a['message'])
        L.append('')

    if alerts.get('newCoupons'):
        L.append('## 本次新领')
        for c in alerts['newCoupons']:
            saving = ('省 ¥%.1f' % c['saving']) if c.get('saving') is not None else '金额未知'
            L.append('- [%s] %s ｜ %s ｜ %s 到期'
                     % (c.get('gradeLabel'), c.get('name'), saving,
                        (c.get('validTo') or '')[:10]))
        L.append('')

    L.append('## 先看这几张（紧迫度 → 价值 排序）')
    L.append('')
    L.append('| 券 | 等级 | 剩余 | 可用餐段 | 预计省 | 渠道 |')
    L.append('|---|---|---|---|---|---|')
    for c in coupons[:10]:
        days = c.get('daysLeft')
        left = '已过期' if days is None or days < 0 else ('今天' if days == 0 else '%d 天' % days)
        ch = '到店+外送' if len(set(c.get('channels') or [])) >= 2 else \
             ('到店' if 'pickup' in (c.get('channels') or []) else '外送')
        sv = ('¥%.1f' % c['saving']) if c.get('saving') is not None else '未知'
        L.append('| %s | %s %s | %s | %s | %s | %s |'
                 % (c.get('name'), c.get('grade'), c.get('gradeLabel'), left,
                    '、'.join(c.get('periods') or []) or '-', sv, ch))
    L.append('')

    today_usable = [c for c in coupons if c.get('usableToday') and c.get('daysLeft') == 0]
    if today_usable:
        L.append('## 今天就能用掉')
        for c in today_usable:
            L.append('- [%s] %s ｜ %s' % (c.get('gradeLabel'), c.get('name'),
                                          '、'.join(c.get('periods') or []) or '全天'))
        L.append('')

    resident = [c for c in coupons if (c.get('daysLeft') or 0) > 30]
    if resident:
        L.append('## 长期券（30 天以上）')
        L.append('- ' + ' · '.join('%s（0元）' % c.get('name') if c.get('isFree')
                                   else c.get('name') for c in resident))
        L.append('')

    unknown = [c for c in coupons if not c.get('savingKnown')]
    if unknown:
        L.append('> 注：%d 张券未能估算原价（无菜单实价、也无可匹配基线），'
                 '节省金额标记为「未知」，未纳入金额评分。' % len(unknown))
        L.append('')

    L.append('---')
    L.append('本简报仅用于券的资产管理，不构成下单建议；本工具不会自动下单。')
    return '\n'.join(L)


# ---------------------------------------------------------------- 子命令

def cmd_all(args):
    payload = read_payload(args.input)
    today = resolve_today(args.today)
    outdir = args.outdir or '.'
    os.makedirs(outdir, exist_ok=True)

    config = load_config(args.config)
    tiered = tier_payload(payload, today=today,
                          now=resolve_now(getattr(args, 'now', None), today),
                          config=config)
    tiered['generatedAt'] = datetime.now().strftime('%Y-%m-%d %H:%M')

    ledger_path = args.ledger or DEFAULT_LEDGER
    ledger = load_ledger(ledger_path)
    alerts = check(tiered, ledger, today=today)
    if not args.no_write:
        save_ledger(ledger_path, ledger)

    write_text(os.path.join(outdir, 'tiered.json'),
               json.dumps(tiered, ensure_ascii=False, indent=2) + '\n')
    write_text(os.path.join(outdir, 'alerts.json'),
               json.dumps(alerts, ensure_ascii=False, indent=2) + '\n')
    write_text(os.path.join(outdir, 'calendar.html'), render_html(tiered, days=args.days, start=today))
    if not args.no_ics:
        write_text(os.path.join(outdir, 'calendar.ics'), build_ics(tiered, today=today))
    report = build_report(tiered, alerts, today)
    write_text(os.path.join(outdir, 'report.md'), report)

    print(report)
    print('')
    print('已输出到 %s：tiered.json / alerts.json / calendar.html'
          '%s / report.md' % (outdir, '' if args.no_ics else ' / calendar.ics'))
    return 0


def cmd_parse(args):
    result = parse_payload(read_payload(args.input))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        write_text(args.out, text + '\n')
        print('已解析 %d 张券 -> %s' % (result['count'], args.out))
    else:
        print(text)
    return 0


def cmd_tier(args):
    today = resolve_today(args.today)
    result = tier_payload(read_payload(args.input), today=today,
                          now=resolve_now(getattr(args, 'now', None), today),
                          config=load_config(args.config))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        write_text(args.out, text + '\n')
        s = result['gradeSummary']
        print('已分级 %d 张：S=%d A=%d B=%d C=%d -> %s'
              % (result['count'], s.get('S', 0), s.get('A', 0), s.get('B', 0), s.get('C', 0),
                 args.out))
    else:
        print(text)
    return 0


def cmd_alert(args):
    today = resolve_today(args.today)
    ledger_path = args.ledger or DEFAULT_LEDGER
    ledger = load_ledger(ledger_path)
    result = check(read_payload(args.input), ledger, today=today)
    if not args.no_write:
        save_ledger(ledger_path, ledger)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        write_text(args.out, text + '\n')
        print('新券 %d 张，提醒 %d 条 -> %s' % (result['newCount'], result['alertCount'], args.out))
    else:
        print(text)
    return 0


def cmd_calendar(args):
    payload = read_payload(args.input)
    today = resolve_today(args.today)
    outdir = args.outdir or '.'
    os.makedirs(outdir, exist_ok=True)
    payload['generatedAt'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    write_text(os.path.join(outdir, 'calendar.html'), render_html(payload, days=args.days, start=today))
    print('已生成日历 %s' % os.path.join(outdir, 'calendar.html'))
    if not args.no_ics:
        write_text(os.path.join(outdir, 'calendar.ics'), build_ics(payload, today=today))
        print('已生成日历文件 %s' % os.path.join(outdir, 'calendar.ics'))
    return 0


def cmd_selftest(args):
    """用内置样例数据跑完整流水线，验证所有模块可正常工作。"""
    if not os.path.exists(SAMPLE):
        print('找不到样例数据 %s' % SAMPLE)
        return 1

    today = resolve_today(args.today)
    tmp = args.outdir or os.path.join(SKILL_ROOT, 'selftest_out')
    os.makedirs(tmp, exist_ok=True)

    payload = read_payload(SAMPLE)

    print('=' * 60)
    print('样例数据：%s' % SAMPLE)
    print('=' * 60)

    parsed = parse_payload(payload)
    print('\n[1/4] 解析：%d 张券，去重 %d 张'
          % (parsed['count'], parsed['duplicatesRemoved']))
    for c in parsed['coupons']:
        print('   %-22s 品类=%-8s 折扣=%-6s 时段=%s%s'
              % (c['name'], c['category'],
                 ('%.1f折' % (c['discountRate'] * 10)) if c['discountRate'] is not None else '-',
                 c['windows'] if c['windows'] else '全天',
                 ' 跨天' if c['crossDay'] else ''))
        assert c['validFrom'] and c['validTo'], '有效期解析失败：%s' % c['name']

    tiered = tier_payload(payload, today=today)
    s = tiered['gradeSummary']
    print('\n[2/4] 分级：S=%d A=%d B=%d C=%d' % (s.get('S', 0), s.get('A', 0),
                                                s.get('B', 0), s.get('C', 0)))
    for c in tiered['coupons']:
        sv = ('省¥%.1f' % c['saving']) if c.get('saving') is not None else '金额未知'
        free_tag = '[0元]' if c.get('isFree') else '    '
        print('   %s[%s] %-22s V=%.3f 剩%-4s %-8s %s'
              % (free_tag, c['grade'], c['name'], c['valueScore'],
                 ('%d天' % c['daysLeft']) if c['daysLeft'] is not None else '-',
                 c['urgencyLabel'], sv))

    # 免费券必须全部是极品券——这条规则由自测守门
    free_all = [c for c in tiered['coupons'] if c.get('isFree')]
    bad_free = [c['name'] for c in free_all if c.get('grade') != 'S']
    assert not bad_free, '0 元券未被判为极品券：%s' % bad_free
    print('   免费券 %d 张，均已判为极品券 ✅' % len(free_all))

    ledger_file = os.path.join(tmp, '_selftest_ledger.json')
    if os.path.exists(ledger_file):
        os.remove(ledger_file)
    alerts = check(tiered, load_ledger(ledger_file), today=today)
    print('\n[3/4] 台账：新券 %d 张，提醒 %d 条' % (alerts['newCount'], alerts['alertCount']))
    for a in alerts['alerts']:
        print('   %s' % a['message'])

    from calendar_builder import render_html as rh, build_ics as bi
    tiered['generatedAt'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    html_text = rh(tiered, days=7, start=today)
    ics_text = bi(tiered, today=today)
    write_text(os.path.join(tmp, 'calendar.html'), html_text)
    write_text(os.path.join(tmp, 'calendar.ics'), ics_text)

    # 连续多天可用的券必须合并成横条（colspan），不允许逐格重复
    import re as _re
    span_cells = _re.findall(r'colspan="(\d+)"', html_text)
    max_span = max((int(x) for x in span_cells), default=1)
    assert max_span > 1, '日历没有产生任何跨天横条，连续多天的券被逐格重复了'
    print('   日历横条合并生效，最长一条连用 %d 天 ✅' % max_span)
    report = build_report(tiered, alerts, today)
    write_text(os.path.join(tmp, 'report.md'), report)
    print('\n[4/4] 输出：calendar.html (%d 字节) / calendar.ics (%d 字节) / report.md (%d 字节)'
          % (len(html_text), len(ics_text), len(report)))

    print('\n' + '=' * 60)
    print('自测通过 ✅  输出目录：%s' % tmp)
    print('=' * 60)
    print('\n' + report)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='麦麦省心 - 麦当劳优惠券自动托管')
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p, need_input=True):
        if need_input:
            p.add_argument('--input', '-i', help='输入 JSON；省略则读 stdin')
        p.add_argument('--outdir', '-o', help='输出目录')
        p.add_argument('--out', help='输出文件')
        p.add_argument('--today', help='覆盖今天日期 YYYY-MM-DD（测试用）')
        p.add_argument('--now', help='覆盖当前时间 HH:MM（判断当天是否还来得及用）')
        p.add_argument('--config', help='自定义分级配置 JSON')
        p.add_argument('--ledger', help='台账文件路径')
        p.add_argument('--days', type=int, default=7, help='日历展示天数')
        p.add_argument('--no-ics', action='store_true', help='不生成 .ics')
        p.add_argument('--no-write', action='store_true', help='不写台账')

    common(sub.add_parser('all', help='跑完整流水线'))
    common(sub.add_parser('parse', help='只解析'))
    common(sub.add_parser('tier', help='只分级'))
    common(sub.add_parser('alert', help='只检查台账与提醒'))
    common(sub.add_parser('calendar', help='只生成日历'))
    common(sub.add_parser('selftest', help='用内置样例自测'), need_input=False)

    args = ap.parse_args(argv)
    return {
        'all': cmd_all, 'parse': cmd_parse, 'tier': cmd_tier,
        'alert': cmd_alert, 'calendar': cmd_calendar, 'selftest': cmd_selftest,
    }[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
