#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alert_checker.py - 新券检测 + 极品券临期提醒，带台账去重。

台账（ledger）记录每张券的指纹、首见日、最后出现日、最近等级与已提醒级别，
保证「同一张券、同一提醒级别」只提醒一次。

用法：
    python3 alert_checker.py --input tiered.json
    python3 alert_checker.py --input tiered.json --ledger data/coupon_ledger.json --out alerts.json
"""

import argparse
import json
import os
import sys
from datetime import date, datetime

try:
    from value_tier import load_config, tier_payload
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from value_tier import load_config, tier_payload

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER = os.path.join(HERE, '..', 'data', 'coupon_ledger.json')

ALERT_GRADES = ('S',)          # 只有极品券触发主动提醒
ALERT_URGENCIES = ('critical', 'high', 'high')  # 特急 / 临期


def load_ledger(path):
    if path and os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('coupons'), dict):
                return data
        except (ValueError, OSError):
            pass
    return {'version': 1, 'coupons': {}}


def save_ledger(path, ledger):
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)


def check(tiered_payload, ledger, today=None, grades=ALERT_GRADES):
    today = today or date.today()
    today_str = today.isoformat()
    coupons = tiered_payload.get('coupons') or []

    new_coupons, alerts, high_value = [], [], []

    for c in coupons:
        fp = c.get('fingerprint')
        if not fp:
            continue
        prev = ledger['coupons'].get(fp)
        if prev is None:
            new_coupons.append({
                'name': c.get('name'),
                'grade': c.get('grade'),
                'gradeLabel': c.get('gradeLabel'),
                'validTo': c.get('validTo'),
                'saving': c.get('saving'),
                'daysLeft': c.get('daysLeft'),
            })

        if c.get('grade') in grades:
            high_value.append({
                'name': c.get('name'),
                'validTo': c.get('validTo'),
                'daysLeft': c.get('daysLeft'),
                'saving': c.get('saving'),
                'periods': c.get('periods'),
            })

        urgency = c.get('urgency')
        alerted = (prev or {}).get('alertedAt') or {}
        if (c.get('grade') in grades and urgency in ALERT_URGENCIES
                and urgency not in alerted):
            alerts.append({
                'fingerprint': fp,
                'name': c.get('name'),
                'grade': c.get('grade'),
                'gradeLabel': c.get('gradeLabel'),
                'validTo': c.get('validTo'),
                'daysLeft': c.get('daysLeft'),
                'saving': c.get('saving'),
                'periods': c.get('periods'),
                'channels': c.get('channels'),
                'level': urgency,
                'message': build_message(c),
            })

    # 回写台账
    for c in coupons:
        fp = c.get('fingerprint')
        if not fp:
            continue
        prev = ledger['coupons'].setdefault(fp, {})
        prev.setdefault('firstSeen', today_str)
        prev['lastSeen'] = today_str
        prev['name'] = c.get('name')
        prev['validTo'] = c.get('validTo')
        prev['lastGrade'] = c.get('grade')
        prev['lastValueScore'] = c.get('valueScore')
        prev.setdefault('alertedAt', {})
        if c.get('grade') in grades and c.get('urgency') in ALERT_URGENCIES:
            prev['alertedAt'][c['urgency']] = today_str

    # 清理已消失很久的券（保留 90 天）
    cutoff = today.toordinal() - 90
    for fp in list(ledger['coupons'].keys()):
        ls = ledger['coupons'][fp].get('lastSeen')
        try:
            if datetime.strptime(ls, '%Y-%m-%d').date().toordinal() < cutoff:
                del ledger['coupons'][fp]
        except (TypeError, ValueError):
            pass

    alerts.sort(key=lambda a: (a.get('daysLeft') if a.get('daysLeft') is not None else 999,
                               -(a.get('saving') or 0)))

    return {
        'date': today_str,
        'newCoupons': new_coupons,
        'newCount': len(new_coupons),
        'highValueCoupons': high_value,
        'alerts': alerts,
        'alertCount': len(alerts),
    }


def build_message(c):
    days = c.get('daysLeft')
    if days is None:
        when = '即将到期'
    elif days <= 0:
        when = '今天最后一天'
    elif days == 1:
        when = '明天到期'
    else:
        when = '%d 天后到期' % days
    parts = ['【极品券提醒】%s %s' % (c.get('name'), when)]
    if c.get('saving') is not None:
        parts.append('预计省 ¥%.1f' % c['saving'])
    if c.get('periods'):
        parts.append('可用餐段：%s' % '、'.join(c['periods']))
    return '｜'.join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description='券台账：新券检测 + 临期提醒')
    ap.add_argument('--input', '-i', help='分级后的 JSON；省略则读 stdin')
    ap.add_argument('--ledger', default=DEFAULT_LEDGER, help='台账文件路径')
    ap.add_argument('--out', '-o', help='提醒结果输出 JSON；省略则写 stdout')
    ap.add_argument('--today', help='覆盖今天日期 YYYY-MM-DD')
    ap.add_argument('--no-write', action='store_true', help='只检查，不写台账')
    args = ap.parse_args(argv)

    if args.input:
        with open(args.input, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)

    today = datetime.strptime(args.today, '%Y-%m-%d').date() if args.today else None
    ledger = load_ledger(args.ledger)
    result = check(payload, ledger, today=today)

    if not args.no_write:
        save_ledger(args.ledger, ledger)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
        print('新券 %d 张，提醒 %d 条 -> %s' %
              (result['newCount'], result['alertCount'], args.out))
    else:
        print(text)


if __name__ == '__main__':
    main()
