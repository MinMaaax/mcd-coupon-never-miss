#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""value_tier.py - 券价值分级引擎。

给每张券算一个价值分 V，映射到 S/A/B/C 四个等级；
另外单独算一个紧迫度 U（剩余天数），两者互不干扰。

用法：
    python3 value_tier.py --input coupons.json
    python3 value_tier.py --input coupons.json --today 2026-10-10 --out tiered.json
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

try:
    from coupon_parser import parse_payload
except ImportError:  # 允许从其他目录导入
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from coupon_parser import parse_payload

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, '..', 'data', 'tiering_config.json')

# 兜底配置：即使 data/tiering_config.json 缺失也能跑
FALLBACK_CONFIG = {
    'weights': {'saving': 1.2, 'category': 0.8, 'discount': 0.6, 'channel': 0.4},
    'categoryScores': {
        'combo': 1.0, 'burger': 0.8, 'drink': 0.6,
        'side': 0.4, 'dessert': 0.3, 'sauce': 0.15, 'unknown': 0.5,
    },
    'savingCap': 30.0,
    'grades': [
        {'grade': 'S', 'label': '极品券', 'min': 0.78},
        {'grade': 'A', 'label': '精品券', 'min': 0.50},
        {'grade': 'B', 'label': '普通券', 'min': 0.35},
        {'grade': 'C', 'label': '渣渣券', 'min': 0.0},
    ],
    'urgencyDays': {'critical': 1, 'high': 3, 'medium': 7},
    'freeCoupon': {'enabled': True, 'forceGrade': 'S', 'forceScore': 1.0},
    'priceBaseline': {'entries': [], 'byCategory': {}},
}

MEAL_PERIODS = [
    ('早餐', '05:00', '10:30'),
    ('午市', '10:30', '14:00'),
    ('下午茶', '14:00', '17:00'),
    ('晚市', '17:00', '21:00'),
    ('夜宵', '21:00', '05:00'),
]


def load_config(path=None):
    cfg = json.loads(json.dumps(FALLBACK_CONFIG))
    p = path or DEFAULT_CONFIG
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ---------------------------------------------------------------- 打分

def score_saving(saving, cap):
    """节省金额分：0 元 -> 0，cap 元及以上 -> 1.0。"""
    if saving is None:
        return 0.5, 'unknown'
    return min(max(saving, 0.0) / float(cap), 1.0), 'ok'


def score_discount(rate):
    """折扣力度分。"""
    if rate is None:
        return 0.5
    if rate <= 0.01:
        return 1.0
    if rate <= 0.50:
        return 0.9
    if rate <= 0.65:
        return 0.6
    if rate <= 0.80:
        return 0.4
    return 0.25


def score_channel(channels):
    return 1.0 if len(set(channels or [])) >= 2 else 0.7


# ---------------------------------------------------------------- 原价

def resolve_original_price(coupon, config):
    """推断券的原价。返回 (price, source)。

    优先级：
      meal-price        由上游从 query-meals 拿到的门店实价（最准）
      baseline-keyword  price_baseline 里最长关键词匹配
      baseline-category 按品类取兜底价
      unknown           拿不到，不猜
    """
    if coupon.get('originalPrice') is not None:
        try:
            return float(coupon['originalPrice']), 'meal-price'
        except (TypeError, ValueError):
            pass

    name = coupon.get('name') or ''
    cat = coupon.get('category') or 'unknown'
    baseline = config.get('priceBaseline') or {}
    best = None
    for entry in baseline.get('entries') or []:
        kw = entry.get('match') or ''
        if not kw or kw not in name:
            continue
        # 条目声明了品类时，只在该品类下参与匹配，
        # 避免「厚松饼」这类单品价被误用到「厚松饼套餐」上
        ecat = entry.get('category')
        if ecat and ecat != cat:
            continue
        if best is None or len(kw) > len(best.get('match') or ''):
            best = entry
    if best:
        try:
            return float(best['price']), 'baseline-keyword'
        except (KeyError, TypeError, ValueError):
            pass

    p = (baseline.get('byCategory') or {}).get(cat)
    if p is not None:
        try:
            return float(p), 'baseline-category'
        except (TypeError, ValueError):
            pass

    return None, 'unknown'


# ---------------------------------------------------------------- 时间

def _parse_dt(s):
    if not s:
        return None
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _to_minute(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def _window_segments(windows):
    """把时段列表拆成 [0,1440) 内的分钟区间集合，跨天时段拆成两段。"""
    segs = []
    for w in windows or []:
        try:
            a = _to_minute(w[0])
            b = _to_minute(w[1])
        except (IndexError, ValueError):
            continue
        if a <= b:
            segs.append((a, b if b > a else a + 1))
        else:
            segs.append((a, 1440))
            segs.append((0, b))
    return segs


def _hits(seg_a, seg_b):
    for a1, b1 in seg_a:
        for a2, b2 in seg_b:
            if a1 < b2 and b1 > a2:
                return True
    return False


def usable_periods(coupon):
    """返回这张券在其有效期内可用的餐段名列表。"""
    segs = _window_segments(coupon.get('windows'))
    if not segs:
        return [p[0] for p in MEAL_PERIODS]
    out = []
    for name, s, e in MEAL_PERIODS:
        psegs = _window_segments([[s, e]])
        if _hits(segs, psegs):
            out.append(name)
    return out


def compute_urgency(days_left, config):
    th = config.get('urgencyDays') or {}
    if days_left < 0:
        return 'expired', '已过期'
    if days_left <= th.get('critical', 1):
        return 'critical', '特急'
    if days_left <= th.get('high', 3):
        return 'high', '临期'
    if days_left <= th.get('medium', 7):
        return 'medium', '留意'
    return 'normal', '常规'


def grade_of(score, config):
    for g in config.get('grades') or []:
        if score >= g.get('min', 0):
            return g.get('grade', 'C'), g.get('label', '渣渣券')
    return 'C', '渣渣券'


def grade_label_of(grade, config):
    """按等级字母取标签，供免费券硬规则直接指定等级时使用。"""
    for g in config.get('grades') or []:
        if g.get('grade') == grade:
            return g.get('label', grade)
    return {'S': '极品券', 'A': '精品券', 'B': '普通券', 'C': '渣渣券'}.get(grade, grade)


# ---------------------------------------------------------------- 主流程

def tier_coupon(coupon, today, config, now=None):
    """给单张券打分 + 定级 + 算紧迫度。"""
    weights = config.get('weights') or {}
    cat_scores = config.get('categoryScores') or {}

    orig, orig_src = resolve_original_price(coupon, config)

    bogo = bool(coupon.get('bogo'))
    face_is_price = (coupon.get('faceSemantics') == 'price'
                     and coupon.get('facePrice') is not None)

    # 实际到手价与实际省下的钱
    effective = None
    saving = None
    if bogo:
        if face_is_price:
            # 用券价即「两份一起」的价格
            effective = float(coupon['facePrice'])
            if orig is not None:
                saving = orig * 2.0 - effective
        else:
            # 付一份的钱拿两份
            effective = orig
            saving = orig
    else:
        if face_is_price:
            effective = float(coupon['facePrice'])
        elif coupon.get('discountRate') is not None and orig is not None:
            effective = orig * float(coupon['discountRate'])
        if orig is not None and effective is not None:
            saving = orig - effective

    if saving is not None:
        saving = max(saving, 0.0)

    s_saving, saving_state = score_saving(saving, config.get('savingCap', 30.0))
    s_category = float(cat_scores.get(coupon.get('category') or 'unknown', 0.5))

    # 折扣分：券名里写了「N折」就用它；否则用「到手价 / 原价」反推。
    # 券面直接给了「用券价格」时不能退回中性分，否则会低估这类券的力度。
    rate_for_score = coupon.get('discountRate')
    if rate_for_score is None and orig is not None and effective is not None and orig > 0:
        denom = orig * 2.0 if bogo else orig
        if denom > 0:
            rate_for_score = max(min(effective / denom, 1.0), 0.0)
    s_discount = score_discount(rate_for_score)

    s_channel = score_channel(coupon.get('channels'))

    total_w = (weights.get('saving', 1.2) + weights.get('category', 0.8) +
               weights.get('discount', 0.6) + weights.get('channel', 0.4))
    raw = (weights.get('saving', 1.2) * s_saving +
           weights.get('category', 0.8) * s_category +
           weights.get('discount', 0.6) * s_discount +
           weights.get('channel', 0.4) * s_channel)
    value_score = round(raw / total_w, 4) if total_w else 0.0

    # 免费券硬规则（0 元券一律极品券）。
    # 「免费」是一种独立性质，不该被加权分稀释：一张 0 元大米派省的钱虽少，
    # 但「不花钱拿到」的价值高于任何折扣券，所以直接锁定最高等级，
    # 且不依赖原价基线（原价未知也照样判极品）。
    free_rule = config.get('freeCoupon') or {}
    if coupon.get('isFree') and free_rule.get('enabled', True):
        grade = free_rule.get('forceGrade', 'S')
        grade_label = grade_label_of(grade, config)
        try:
            value_score = round(float(free_rule.get('forceScore', 1.0)), 4)
        except (TypeError, ValueError):
            value_score = 1.0
        grade_reason = 'free'
    else:
        grade, grade_label = grade_of(value_score, config)
        grade_reason = 'score'

    dt_to = _parse_dt(coupon.get('validTo'))
    days_left = (dt_to.date() - today).days if dt_to else None
    urgency, urgency_label = compute_urgency(
        days_left if days_left is not None else 999, config)

    out = dict(coupon)
    out.update({
        'originalPrice': round(orig, 2) if orig is not None else None,
        'originalPriceSource': orig_src,
        'effectivePrice': round(effective, 2) if effective is not None else None,
        'discountRateEffective': round(rate_for_score, 4) if rate_for_score is not None else None,
        'saving': round(saving, 2) if saving is not None else None,
        'savingKnown': saving is not None,
        'scores': {
            'saving': round(s_saving, 4),
            'category': round(s_category, 4),
            'discount': round(s_discount, 4),
            'channel': round(s_channel, 4),
        },
        'valueScore': value_score,
        'grade': grade,
        'gradeLabel': grade_label,
        'gradeReason': grade_reason,
        'daysLeft': days_left,
        'urgency': urgency,
        'urgencyLabel': urgency_label,
        'periods': usable_periods(coupon),
        'usableToday': True,
    })

    # 今天是否还来得及用（当天最后一档时段是否已过）
    if days_left == 0 and now is not None:
        segs = _window_segments(coupon.get('windows'))
        if segs:
            now_min = now.hour * 60 + now.minute
            out['usableToday'] = any(b > now_min for _, b in segs)
    return out


def sort_key(c):
    """排序：先按紧迫度分桶，桶内按价值分降序，同分再按省下的钱降序。

    免费券的 valueScore 会被拉平到 1.0，所以「省得多」这个次键决定了
    0 元月堡会排在 0 元大米派前面。
    """
    order = {'critical': 0, 'high': 1, 'medium': 2, 'normal': 3, 'expired': 4}
    saving = c.get('saving')
    return (order.get(c.get('urgency'), 9),
            -float(c.get('valueScore') or 0),
            -(float(saving) if saving is not None else -1.0))


def tier_payload(payload, today=None, now=None, config=None):
    config = config or load_config()
    if today is None:
        today = date.today()
    parsed = parse_payload(payload)
    tiered = [tier_coupon(c, today, config, now) for c in parsed['coupons']]
    tiered = [c for c in tiered if c.get('urgency') != 'expired']
    tiered.sort(key=sort_key)

    summary = {'S': 0, 'A': 0, 'B': 0, 'C': 0}
    for c in tiered:
        summary[c['grade']] = summary.get(c['grade'], 0) + 1

    return {
        'fetchedAt': parsed.get('fetchedAt'),
        'today': today.isoformat(),
        'count': len(tiered),
        'duplicatesRemoved': parsed.get('duplicatesRemoved', 0),
        'gradeSummary': summary,
        'coupons': tiered,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description='麦当劳券价值分级引擎')
    ap.add_argument('--input', '-i', help='券数据 JSON；省略则读 stdin')
    ap.add_argument('--out', '-o', help='输出 JSON；省略则写 stdout')
    ap.add_argument('--today', help='覆盖今天日期 YYYY-MM-DD（便于测试）')
    ap.add_argument('--now', help='覆盖当前时间 HH:MM（判断当天是否还来得及用）')
    ap.add_argument('--config', help='自定义分级配置 JSON')
    args = ap.parse_args(argv)

    if args.input:
        with open(args.input, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)

    today = datetime.strptime(args.today, '%Y-%m-%d').date() if args.today else None
    now = None
    if args.now:
        h, m = args.now.split(':')
        base = today or date.today()
        now = datetime(base.year, base.month, base.day, int(h), int(m))

    result = tier_payload(payload, today=today, now=now, config=load_config(args.config))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
        s = result['gradeSummary']
        print('已分级 %d 张：S=%d A=%d B=%d C=%d' %
              (result['count'], s.get('S', 0), s.get('A', 0), s.get('B', 0), s.get('C', 0)))
    else:
        print(text)


if __name__ == '__main__':
    main()
