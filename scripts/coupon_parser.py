#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""coupon_parser.py - 把麦当劳券的「自由文本有效期」与券名解析成结构化数据。

纯标准库实现，Python 3.8+ 可直接运行，不需要安装任何第三方依赖。

用法：
    python3 coupon_parser.py --input coupons.json
    python3 coupon_parser.py --input coupons.json --out parsed.json
    cat coupons.json | python3 coupon_parser.py
"""

import argparse
import hashlib
import json
import re
import sys

# ---------------------------------------------------------------- 正则

# 「2026-09-24 10:30」形式的日期时间戳
DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})')
# 「周日」「周三」
WEEKDAY_RE = re.compile(r'周([一二三四五六日天])')
# 「10:30-23:59」
TIME_RE = re.compile(r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})')
# 「¥9.9」「￥9.9」「9.9元」
PRICE_RE = re.compile(r'[¥￥]\s*(\d+(?:\.\d+)?)')
PRICE_YUAN_RE = re.compile(r'(\d+(?:\.\d+)?)\s*元')
# 「4折」「4.5折」
DISCOUNT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*折')
# 「买一送一」
BOGO_RE = re.compile(r'买\s*一\s*送\s*一|买\s*1\s*送\s*1')
# 「半价」「第二份半价」
HALF_RE = re.compile(r'半价')
# 「免费」「免单」「零元」「0元」——负向前瞻避免把「10元」里的「0元」匹配进来
FREE_RE = re.compile(r'免费|免单|白送|零元|(?<![\d.])0\s*元')
# 「0元起」这类营销话术并非真的免费，需排除
FREE_EXCLUDE_RE = re.compile(r'0\s*元\s*起')

WEEKDAY_INDEX = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}

# 品类识别规则，顺序即优先级（先匹配到的胜出）
CATEGORY_RULES = [
    ('combo',   ['套餐', '三件套', '两件套', '四件套', '五件套', '组合', '套装']),
    ('burger',  ['堡', '麦满分', '巨无霸', '双吉', '安格斯', '汉堡', '卷']),
    ('drink',   ['咖啡', '美式', '拿铁', '奶铁', '可乐', '雪碧', '芬达', '奶茶',
                 '奶昔', '冰沙', '果汁', '气泡', '茶']),
    ('dessert', ['麦旋风', '圆筒', '冰淇淋', '圣代', '新地', '甜筒', '蛋糕', '派']),
    ('side',    ['薯条', '薯饼', '鸡块', '鸡翅', '鸡腿', '玉米', '沙拉', '蔬菜', '鸡排']),
    ('sauce',   ['酱', '蘸']),
]

CHANNEL_KEYWORDS = [('到店', 'pickup'), ('外送', 'delivery'), ('麦乐送', 'delivery'), ('得来速', 'pickup')]


# ---------------------------------------------------------------- 有效期解析

def _norm_time(t):
    """把 '9:5' 归一成 '09:05'。"""
    h, m = t.split(':')
    return '%02d:%02d' % (int(h), int(m))


def parse_validity(text):
    """把券的有效期自由文本解析成结构化字段。

    支持实测到的 4 种形态：
      A 日期区间 + 每日时段（可含跨天时段）
        2026-09-24 10:30-2026-12-31 23:59 10:30-23:59 00:00-04:59
      B 纯日期区间
        2026-09-24 00:00-2026-12-31 23:59
      C 单日 + 星期 + 时段
        2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29
      D 任何以上形态的变体

    返回 dict：
      validFrom / validTo : 'YYYY-MM-DD HH:MM' 或 None
      weekdays            : [0..6] 或 None（None 表示不限星期）
      windows             : [['10:30','23:59'], ...] 空列表表示全天可用
      crossDay            : 是否存在跨零点时段
      raw                 : 原文
    """
    text = (text or '').strip()
    out = {
        'raw': text,
        'validFrom': None,
        'validTo': None,
        'weekdays': None,
        'windows': [],
        'crossDay': False,
    }
    if not text:
        return out

    work = text

    stamps = [(m.group(1), m.group(2)) for m in DATE_RE.finditer(work)]
    work = DATE_RE.sub(' ', work)

    wd = WEEKDAY_RE.findall(work)
    if wd:
        out['weekdays'] = sorted({WEEKDAY_INDEX[w] for w in wd})
    work = WEEKDAY_RE.sub(' ', work)

    windows = [[_norm_time(m.group(1)), _norm_time(m.group(2))]
               for m in TIME_RE.finditer(work)]
    out['windows'] = windows
    out['crossDay'] = any(a > b for a, b in windows)

    if stamps:
        out['validFrom'] = '%s %s' % (stamps[0][0], _norm_time(stamps[0][1]))
        last = stamps[-1]
        out['validTo'] = '%s %s' % (last[0], _norm_time(last[1]))

    return out


# ---------------------------------------------------------------- 券名解析

def detect_category(name):
    """按券名判断品类。"""
    n = name or ''
    for cat, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in n:
                return cat
    return 'unknown'


def detect_discount_rate(name, face_price, face_semantics):
    """从券名/券面推断折扣率（1 折 = 0.1，免费 = 0.0）。

    券名里的「N折」优先级最高；其次是「买一送一」「半价」；最后是免费判定。
    返回 float 或 None（无法判断）。
    """
    n = name or ''

    m = DISCOUNT_RE.search(n)
    if m:
        try:
            v = float(m.group(1))
            if 0 < v < 10:
                return round(v / 10.0, 4)
        except ValueError:
            pass

    if BOGO_RE.search(n) or HALF_RE.search(n):
        return 0.5

    if FREE_RE.search(n) and not FREE_EXCLUDE_RE.search(n):
        return 0.0

    if face_semantics == 'price' and face_price is not None and face_price == 0:
        return 0.0

    return None


def detect_free(name, face_price, face_semantics, rate):
    """判断是否为免费券（0 元券）。

    「免费」是一种独立性质，不等同于「折扣很低的券」——所以这里单独识别，
    分级引擎会据此走硬规则（免费一律极品券），不参与加权打分。

    返回 (is_free, signal)：
      'face-price'  券面写明「用券价格 ¥0」——最硬的证据
      'name'        券名含「免费 / 免单 / 零元 / 0元」
      'rate-zero'   折扣率被判定为 0
      None          非免费
    """
    if face_semantics == 'price' and face_price is not None:
        try:
            if float(face_price) == 0.0:
                return True, 'face-price'
        except (TypeError, ValueError):
            pass

    n = name or ''
    if n and FREE_EXCLUDE_RE.search(n):
        return False, None
    if n and FREE_RE.search(n):
        return True, 'name'
    if rate is not None:
        try:
            if float(rate) == 0.0:
                return True, 'rate-zero'
        except (TypeError, ValueError):
            pass
    return False, None


def parse_price(text):
    """解析券面金额。

    返回 (face_price, semantics)：
      semantics = 'price'   -> 文本明确写了「用券价格」，该数字是用券后的价格
      semantics = 'unknown' -> 数字存在但语义不明（例如「用券立享」），
                               后续应以券名折扣反推实际价格，不直接采信该数字
    """
    text = (text or '').strip()
    value = None
    m = PRICE_RE.search(text)
    if m:
        value = float(m.group(1))
    else:
        m = PRICE_YUAN_RE.search(text)
        if m:
            value = float(m.group(1))

    if '用券价格' in text:
        return value, 'price'
    if value is None:
        return None, 'unknown'
    return value, 'unknown'


def detect_channels(tags):
    """渠道解析。返回 (channels, assumed)。"""
    channels = []
    for t in tags or []:
        for kw, ch in CHANNEL_KEYWORDS:
            if kw in t and ch not in channels:
                channels.append(ch)
    if not channels:
        return ['pickup', 'delivery'], True
    return channels, False


def fingerprint(name, validity_text, image_url):
    """券指纹：返回体的券没有 ID，用「券名 + 有效期原文 + 图片」做去重主键。"""
    seed = '%s|%s|%s' % (name or '', validity_text or '', image_url or '')
    return hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]


# ---------------------------------------------------------------- 主解析函数

def parse_coupon(raw):
    """解析单张券。"""
    name = (raw.get('name') or '').strip()
    validity_text = raw.get('validityText') or raw.get('validity') or ''
    price_text = raw.get('priceText') or raw.get('price') or ''
    tags = raw.get('tags') or []
    image_url = raw.get('imageUrl') or ''

    v = parse_validity(validity_text)
    face_price, semantics = parse_price(price_text)
    category = detect_category(name)
    rate = detect_discount_rate(name, face_price, semantics)
    is_free, free_signal = detect_free(name, face_price, semantics, rate)
    channels, channels_assumed = detect_channels(tags)

    return {
        'fingerprint': fingerprint(name, validity_text, image_url),
        'name': name,
        'category': category,
        'bogo': bool(BOGO_RE.search(name)),
        'isFree': is_free,
        'freeSignal': free_signal,
        'facePrice': face_price,
        'faceSemantics': semantics,
        'discountRate': rate,
        'validFrom': v['validFrom'],
        'validTo': v['validTo'],
        'weekdays': v['weekdays'],
        'windows': v['windows'],
        'crossDay': v['crossDay'],
        'rawValidity': v['raw'],
        'channels': channels,
        'channelsAssumed': channels_assumed,
        'receivedText': raw.get('receivedText') or '',
        'imageUrl': image_url,
        'originalPrice': raw.get('originalPrice'),
    }


def parse_payload(payload):
    """解析整个 payload（{'coupons': [...]}）。"""
    coupons = payload.get('coupons') or []
    parsed = [parse_coupon(c) for c in coupons]
    # 按指纹去重，保留首次出现
    seen = set()
    unique = []
    dup = 0
    for c in parsed:
        if c['fingerprint'] in seen:
            dup += 1
            continue
        seen.add(c['fingerprint'])
        unique.append(c)
    return {
        'fetchedAt': payload.get('fetchedAt'),
        'count': len(unique),
        'duplicatesRemoved': dup,
        'coupons': unique,
    }


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description='麦当劳券文本解析器')
    ap.add_argument('--input', '-i', help='券数据 JSON 文件；省略则从 stdin 读取')
    ap.add_argument('--out', '-o', help='输出 JSON 文件；省略则写到 stdout')
    args = ap.parse_args(argv)

    if args.input:
        with open(args.input, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)

    result = parse_payload(payload)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
        print('已解析 %d 张券（去重 %d 张）-> %s' %
              (result['count'], result['duplicatesRemoved'], args.out))
    else:
        print(text)


if __name__ == '__main__':
    main()
