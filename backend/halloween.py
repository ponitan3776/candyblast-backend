# -*- coding: utf-8 -*-
"""
🎃 ハロウィンイベント(期間限定)

* 期間はサーバーの時計で決まる。フロントのアップデートが無くても、終了時刻になれば
  ガチャ・ロード・ミッションは自動で閉じる(終了後 GRACE の間だけ「報酬の受け取り」と「交換所」は使える)。
* 期間は環境変数で変更できる:
    HALLOWEEN_START / HALLOWEEN_END  例: 2026-10-17T00:00:00+09:00 (タイムゾーン省略時は日本時間)
    HALLOWEEN_GRACE_HOURS            終了後の猶予(時間、既定48)
* ユーザーごとの進行状況は users.event_state(JSONB)に保存。イベントIDが変わると自動でリセット。
"""
import os
import json
import time
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import request, jsonify

import hw_content as C            # 🎃 パート2の中身(図鑑・スタンプ・ミッション等の数字と文章)
import minigames as _minigames    # スタンプラリーの対象(ミニゲームの一覧)を自動で取る

JST = ZoneInfo('Asia/Tokyo')
EVENT_ID = 'halloween2026'


def _env_dt(name, default):
    raw = os.environ.get(name)
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=JST)
        except ValueError:
            pass
    return default


START = _env_dt('HALLOWEEN_START', datetime(2026, 10, 17, 0, 0, tzinfo=JST))
END = _env_dt('HALLOWEEN_END', datetime(2026, 11, 8, 0, 0, tzinfo=JST))
try:
    GRACE = timedelta(hours=int(os.environ.get('HALLOWEEN_GRACE_HOURS', '48')))
except ValueError:
    GRACE = timedelta(hours=48)


def now_dt():
    return datetime.now(JST)


def phase(now=None):
    """upcoming(開催前) / active(開催中) / grace(終了後の猶予) / ended(完全終了)"""
    now = now or now_dt()
    if now < START:
        return 'upcoming'
    if now < END:
        return 'active'
    if now < END + GRACE:
        return 'grace'
    return 'ended'


# ===================== ハロウィンガチャ =====================
# ★専用通貨「ハロウィンキャンディ 🍬」で引く(コインは使わない)。
#   アクティブに遊ぶ人で1日およそ500個、のんびりでも約200個たまる想定 → 1回80個なら、期間中に数十〜百数十回引ける
GACHA_COST = 80                # 1回あたりのキャンディ(10連は800)
GACHA_RATE_PER_SKIN = 0.005    # 1スキンあたりの排出率(6種で合計3.0%)
GACHA_PITY = 1000              # 天井(未所持のスキンから優先して確定)
GACHA_DUP_CANDIES = 40         # 被り: 半分が戻ってくる
GACHA_MISS_CANDIES = 8         # ハズレ: 1割が戻ってくる

GACHA_POOL = [
    'hween_pumpkinking', 'hween_vampire', 'hween_ghostparade',
    'hween_witchbrew', 'hween_blackcat', 'hween_candycorn',
    'hween_jackolantern', 'hween_mummy',                       # v3.6 で追加(8種になり、合計4.0%)
]
ROAD_SKINS = {20: 'hween_moonlight', 40: 'hween_eternalnight', 50: 'hween_crimsonmoon', 60: 'hween_grandcrown'}
SHOP_SKINS = ['hween_spiderweb', 'hween_batswarm', 'hween_frankenstein', 'hween_skeletondance']
EXTRA_SKINS = ['hween_trickortreat', 'hween_graveyardfog']     # スタンプラリー完走 / 図鑑コンプリートの報酬
ALL_SKIN_IDS = set(GACHA_POOL) | set(ROAD_SKINS.values()) | set(SHOP_SKINS) | set(EXTRA_SKINS)


# ===================== ハロウィンロード(累計キャンディで進む報酬) =====================
ROAD_TIERS = 60                # v3.6 で40→60段に(41〜60段は後半の長旅)


def road_need(tier):
    return 45 * tier + 3 * tier * tier          # 10段=750 / 20段=2100 / 40段=6600 / 60段=13500


def road_reward(tier):
    if tier in ROAD_SKINS:
        r = {'type': 'skin', 'skinId': ROAD_SKINS[tier]}
        if tier == 40:
            r['extraCoins'] = 20000
        if tier == 60:
            r['extraCoins'] = 40000
        return r
    if tier % 10 == 0:
        return {'type': 'ticket', 'amount': 5}
    if tier % 5 == 0:
        return {'type': 'ticket', 'amount': 2}
    if tier % 3 == 0:
        return {'type': 'bpxp', 'amount': 1500}
    return {'type': 'coins', 'amount': 1000 + 150 * tier}


ROAD = [{'tier': t, 'need': road_need(t), 'reward': road_reward(t)} for t in range(1, ROAD_TIERS + 1)]


# ===================== ミッション(中身は hw_content.py) =====================
DAILY_POOL = C.DAILY_POOL
WEEKLY_POOL = C.WEEKLY_POOL


def _today():
    return now_dt().strftime('%Y-%m-%d')


def _week_key():
    y, w, _ = now_dt().isocalendar()
    return f'{y}-W{w:02d}'


def pick_missions(pool, key, n):
    return random.Random(f'{EVENT_ID}:{key}').sample(pool, n)


def daily_missions(date=None):
    return pick_missions(DAILY_POOL, date or _today(), C.DAILY_COUNT)


def weekly_missions(week=None):
    return pick_missions(WEEKLY_POOL, week or _week_key(), C.WEEKLY_COUNT)


# ===================== 交換所(中身は hw_content.py) =====================
SHOP = C.SHOP
SHOP_BY_ID = {s['id']: s for s in SHOP}

# ログインボーナス(7日サイクル)
LOGIN_CYCLE = 7


def login_reward(day):
    r = {'candies': 20 + 10 * day, 'coins': 500 * day, 'tickets': 0}
    if day == LOGIN_CYCLE:
        r['tickets'] = 1
    return r


# ===================== 状態の読み書き =====================
def norm_state(st):
    """イベントIDが違えば初期化し、日替わり・週替わりの進捗を必要に応じてリセットする"""
    if not isinstance(st, dict) or st.get('id') != EVENT_ID:
        st = {'id': EVENT_ID}
    st.setdefault('candies', 0)
    st.setdefault('totalCandies', 0)
    st.setdefault('tickets', 0)
    st.setdefault('claimedRoad', [])
    st.setdefault('pity', 0)
    st.setdefault('pulls', 0)
    st.setdefault('shopBought', {})
    st.setdefault('streak', 0)
    st.setdefault('lastLogin', None)
    st.setdefault('lastReport', 0)
    # --- v3.6 パート2 ---
    st.setdefault('dex', [])               # 出会ったおばけ
    st.setdefault('dexClaimed', [])        # 受け取った図鑑ごほうび(必要数)
    st.setdefault('stamps', [])            # 遊んだミニゲーム
    st.setdefault('stampClaimed', [])      # 受け取ったスタンプごほうび(文字で保存: '3' '6' … 'all')
    st.setdefault('fortune', None)
    st.setdefault('communityClaimed', [])
    st.setdefault('guestBonus', False)
    st.setdefault('totTotal', 0)
    st.setdefault('harvestTotal', 0)
    st.setdefault('ghostTotal', 0)
    if not isinstance(st.get('patch'), list):
        st['patch'] = []
    while len(st['patch']) < C.PATCH_PLOTS:
        st['patch'].append(None)
    today, wk = _today(), _week_key()
    if not isinstance(st.get('tot'), dict) or st['tot'].get('date') != today:
        st['tot'] = {'date': today, 'used': 0}
    if not isinstance(st.get('ghost'), dict) or st['ghost'].get('date') != today:
        st['ghost'] = {'date': today, 'count': 0, 'last': st.get('ghost', {}).get('last', 0) if isinstance(st.get('ghost'), dict) else 0}
    if not isinstance(st.get('daily'), dict) or st['daily'].get('date') != today:
        st['daily'] = {'date': today, 'stats': {}, 'claimed': []}
    if not isinstance(st.get('weekly'), dict) or st['weekly'].get('week') != wk:
        st['weekly'] = {'week': wk, 'stats': {}, 'claimed': []}
    return st


def _progress(stats, m):
    return min(m['target'], int(stats.get(m['stat'], 0) or 0))


def _mission_view(missions, bucket):
    out = []
    for m in missions:
        p = _progress(bucket['stats'], m)
        out.append({'id': m['id'], 'text': m['text'], 'target': m['target'], 'progress': p,
                    'candies': m['candies'], 'done': p >= m['target'], 'claimed': m['id'] in bucket['claimed']})
    return out


def _add_stat(bucket, stat, amount):
    if stat == 'score_max':
        bucket['stats'][stat] = max(int(bucket['stats'].get(stat, 0) or 0), int(amount))
    else:
        bucket['stats'][stat] = int(bucket['stats'].get(stat, 0) or 0) + int(amount)


def add_stat_both(st, stat, amount):
    _add_stat(st['daily'], stat, amount)
    _add_stat(st['weekly'], stat, amount)


def add_candies(st, amount):
    st['candies'] += amount
    st['totalCandies'] += amount


def candies_for_game(score, lines, tspins, pcs, duel):
    c = 6 + min(30, score // 1200) + min(12, lines // 3) + 8 * min(tspins, 3) + 20 * min(pcs, 2) + (15 if duel else 0)
    return min(c, 90)


def apply_reward(reward, state):
    """報酬を適用する。state = {'coins', 'skins', 'st', 'bpxp'} を直接書き換える。説明文を返す"""
    t = reward['type']
    if t == 'coins':
        state['coins'] += reward['amount']
        return f"🪙{reward['amount']:,}"
    if t == 'ticket':
        state['st']['tickets'] += reward['amount']
        return f"🎟×{reward['amount']}"
    if t == 'bpxp':
        state['bpxp'] += reward['amount']
        return f"🎫XP+{reward['amount']:,}"
    if t == 'skin':
        if reward['skinId'] not in state['skins']:
            state['skins'].append(reward['skinId'])
        extra = reward.get('extraCoins', 0)
        if extra:
            state['coins'] += extra
        return f"スキン({reward['skinId']})" + (f" + 🪙{extra:,}" if extra else '')
    return ''


# ===================== DB(テストで差し替えやすいよう1か所に集約) =====================
def lock_user(cur, user_id):
    cur.execute('SELECT coins, skins, event_state, battlepass_xp FROM users WHERE id = %s FOR UPDATE', [user_id])
    return cur.fetchone()


def save_user(cur, user_id, s):
    cur.execute('UPDATE users SET coins=%s, skins=%s, event_state=%s, battlepass_xp=%s WHERE id=%s',
                [s['coins'], json.dumps(s['skins']), json.dumps(s['st']), s['bpxp'], user_id])


def load_state_from_row(row):
    skins = json.loads(row.get('skins') or '["default"]')
    return {
        'coins': int(row.get('coins') or 0),
        'skins': skins,
        'st': norm_state(row.get('event_state')),
        'bpxp': int(row.get('battlepass_xp') or 0),
    }


def time_info():
    return {
        'id': EVENT_ID, 'phase': phase(),
        'startsAt': START.isoformat(), 'endsAt': END.isoformat(),
        'graceEndsAt': (END + GRACE).isoformat(), 'serverNow': now_dt().isoformat(),
    }



# ===================== v3.6 パート2: ボーナス・図鑑・スタンプ・畑など =====================
MINI_IDS = list(_minigames.GAMES.keys())


def candy_boost(now=None):
    """いまのキャンディ倍率 (倍率, 表示文)。いちばん高いものが1つだけ効く"""
    now = now or now_dt()
    cands = []
    d = C.BOOST_DAYS.get((now.month, now.day))
    if d:
        cands.append(d)
    if now.weekday() >= 5:
        cands.append(C.BOOST_WEEKEND)
    if 20 <= now.hour < 23:
        cands.append(C.BOOST_NIGHT)
    best = (1.0, '')
    for c in cands:
        if c[0] > best[0]:
            best = c
    return best


def days_to_halloween(now=None):
    now = now or now_dt()
    target = datetime(now.year, C.HALLOWEEN_MONTH_DAY[0], C.HALLOWEEN_MONTH_DAY[1], tzinfo=JST)
    return (target.date() - now.date()).days


def dex_entry(creature):
    return {'id': creature[0], 'emoji': creature[1], 'name': creature[2], 'rarity': creature[3]}


def dex_give(st, rare_only=False, rng=random):
    """おばけと出会う。新しければ図鑑に登録、すでに出会っていればキャンディに変わる"""
    pool = [d for d in C.DEX if (d[3] != 'common' or not rare_only)]
    c = rng.choices(pool, weights=[d[4] for d in pool])[0]
    out = dex_entry(c)
    if c[0] in st['dex']:
        out.update(new=False, candies=C.DEX_DUP_CANDIES[c[3]])
        add_candies(st, out['candies'])
    else:
        st['dex'].append(c[0])
        out.update(new=True, candies=0)
        add_stat_both(st, 'dex_new', 1)
    return out


def stamp_steps():
    """[(キー文字, 必要数, ごほうび)] 。'all' はミニゲーム全種"""
    out, seen = [], set()
    for key in C.STAMP_STEPS:
        need = len(MINI_IDS) if key == 'all' else int(key)
        need = min(need, len(MINI_IDS))
        k = 'all' if key == 'all' else str(key)
        if need in seen and k != 'all':
            continue
        seen.add(need)
        out.append((k, need, C.STAMP_REWARDS[key]))
    return out


def stamp_add(st, game_id):
    if game_id in MINI_IDS and game_id not in st['stamps']:
        st['stamps'].append(game_id)
        return True
    return False


def add_kind(bucket, game_id):
    kinds = bucket.setdefault('kinds', [])
    if game_id not in kinds:
        kinds.append(game_id)
    bucket['stats']['mini_kinds'] = len(kinds)


def patch_view(st):
    now_ts = time.time()
    out = []
    for i, p in enumerate(st['patch']):
        if not p or p.get('kind') not in C.PATCH_BY_ID:
            out.append({'idx': i, 'empty': True})
            continue
        k = C.PATCH_BY_ID[p['kind']]
        ready_at = float(p.get('at') or 0) + k[3]
        out.append({'idx': i, 'empty': False, 'kind': k[0], 'name': k[1], 'emoji': k[2], 'candies': k[4],
                    'readyAt': ready_at, 'ripe': now_ts >= ready_at, 'secLeft': max(0, int(ready_at - now_ts))})
    return out


def dex_steps_view(st):
    n = len(st['dex'])
    return [{'need': r['need'], 'reward': r['reward'], 'reached': n >= r['need'], 'claimed': r['need'] in st['dexClaimed']}
            for r in C.DEX_REWARDS]


def stamp_steps_view(st):
    n = len(st['stamps'])
    return [{'key': k, 'need': need, 'reward': r, 'reached': n >= need, 'claimed': k in st['stampClaimed']}
            for k, need, r in stamp_steps()]


def extra_view(s):
    """v3.6 パート2 の画面用データ(state に足して返す)"""
    st = s['st']
    today = _today()
    mult, label = candy_boost()
    tot = st['tot']
    free_left = max(0, C.TOT_FREE_PER_DAY - tot['used'])
    ft = st['fortune'] if isinstance(st.get('fortune'), dict) and st['fortune'].get('date') == today else None
    patch = patch_view(st)
    dsteps, ssteps = dex_steps_view(st), stamp_steps_view(st)
    dv, mv = _mission_view(daily_missions(), st['daily']), _mission_view(weekly_missions(), st['weekly'])
    lg_done = st['lastLogin'] == today
    badges = {
        'home': 0 if lg_done else 1,
        'road': sum(1 for t in ROAD if st['totalCandies'] >= t['need'] and t['tier'] not in st['claimedRoad']),
        'missions': sum(1 for m in dv + mv if m['done'] and not m['claimed']),
        'tot': (1 if tot['used'] < C.TOT_MAX_PER_DAY and free_left > 0 else 0) + (0 if ft else 1),
        'patch': sum(1 for p in patch if not p['empty'] and p['ripe']) or (1 if any(p['empty'] for p in patch) else 0),
        'dex': sum(1 for d in dsteps if d['reached'] and not d['claimed']),
        'stamps': sum(1 for d in ssteps if d['reached'] and not d['claimed']),
    }
    return {
        'boost': {'mult': mult, 'label': label},
        'daysToHalloween': days_to_halloween(),
        'dexList': [dex_entry(d) for d in C.DEX],
        'dex': {'owned': st['dex'], 'total': len(C.DEX), 'steps': dsteps},
        'stamps': {'games': MINI_IDS, 'got': st['stamps'], 'steps': ssteps},
        'tot': {'used': tot['used'], 'freeLeft': free_left, 'cost': C.TOT_EXTRA_COST, 'max': C.TOT_MAX_PER_DAY,
                'table': [{'title': t[3], 'weight': t[1]} for t in C.TOT_TABLE]},
        'fortune': ft,
        'patch': {'plots': patch, 'kinds': [{'id': k[0], 'name': k[1], 'emoji': k[2], 'sec': k[3], 'candies': k[4]} for k in C.PATCH_KINDS]},
        'ghost': {'left': max(0, C.GHOST_MAX_PER_DAY - st['ghost']['count']), 'max': C.GHOST_MAX_PER_DAY},
        'guestBonusUsed': bool(st['guestBonus']),
        'badges': badges,
    }


def wallet(s):
    return {'coins': s['coins'], 'candies': s['st']['candies'], 'tickets': s['st']['tickets'],
            'totalCandies': s['st']['totalCandies'], 'ownedSkins': s['skins'], 'battlepassXp': s['bpxp']}


def roll_tot(rng=random):
    t = rng.choices(C.TOT_TABLE, weights=[x[1] for x in C.TOT_TABLE])[0]
    return {'kind': t[0], 'title': t[3], 'give': dict(t[2])}


def fortune_roll(user_seed, date):
    rng = random.Random(f'{EVENT_ID}:fortune:{user_seed}:{date}')
    f = rng.choices(C.FORTUNES, weights=[x[1] for x in C.FORTUNES])[0]
    return {'date': date, 'rank': f[0], 'candies': f[2], 'text': f[3],
            'item': rng.choice(C.LUCKY_ITEMS), 'color': rng.choice(C.LUCKY_COLORS), 'number': rng.randint(1, 99)}


# ===================== ルート登録 =====================
def register(app, db_query, db_transaction, require_auth, ApiError):

    def fail(e):
        return jsonify({'error': e.message}), e.status

    def guard(fn):
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except ApiError as e:
                return fail(e)
            except Exception as err:
                print(f'halloween error: {err}')
                return jsonify({'error': str(err) or 'サーバーエラー'}), 500
        wrapper.__name__ = fn.__name__
        return wrapper

    def user_view(s):
        st = s['st']
        today = _today()
        yesterday = (now_dt() - timedelta(days=1)).strftime('%Y-%m-%d')
        if st['lastLogin'] == today:
            streak_day = ((st['streak'] - 1) % LOGIN_CYCLE) + 1                # 今日受け取った日
        else:
            next_streak = st['streak'] + 1 if st['lastLogin'] == yesterday else 1
            streak_day = ((next_streak - 1) % LOGIN_CYCLE) + 1                 # これから受け取る日
        total = st['totalCandies']
        v = {
            **time_info(),
            'coins': s['coins'], 'ownedSkins': s['skins'],
            'candies': st['candies'], 'totalCandies': total, 'tickets': st['tickets'],
            'login': {'claimedToday': st['lastLogin'] == today, 'streak': st['streak'], 'day': streak_day,
                      'cycle': LOGIN_CYCLE, 'rewards': [login_reward(d) for d in range(1, LOGIN_CYCLE + 1)]},
            'road': [{**t, 'reached': total >= t['need'], 'claimed': t['tier'] in st['claimedRoad']} for t in ROAD],
            'daily': _mission_view(daily_missions(), st['daily']),
            'weekly': _mission_view(weekly_missions(), st['weekly']),
            'shop': [{**{k: v for k, v in it.items() if k != 'give'}, 'bought': st['shopBought'].get(it['id'], 0),
                      'give': it['give']} for it in SHOP],
            'gacha': {'cost': GACHA_COST, 'ratePerSkin': GACHA_RATE_PER_SKIN,
                      'rateTotal': round(GACHA_RATE_PER_SKIN * len(GACHA_POOL), 4), 'pool': GACHA_POOL,
                      'pity': st['pity'], 'pityThreshold': GACHA_PITY, 'pulls': st['pulls'],
                      'dupCandies': GACHA_DUP_CANDIES, 'missCandies': GACHA_MISS_CANDIES, 'currency': 'candy'},
        }
        v.update(extra_view(s))
        return v

    # --- 公開: 開催状況(ログイン不要。フロントはこれを見て、イベント画面・ガチャ・UIテーマを出し分ける) ---
    @app.get('/api/event/status')
    def event_status():
        return jsonify({'halloween': time_info()})

    @app.get('/api/event/halloween/state')
    @guard
    def hw_state():
        user_id = require_auth()
        with db_transaction() as cur:
            row = lock_user(cur, user_id)
            if not row:
                raise ApiError('ユーザーが見つかりません', 404)
            s = load_state_from_row(row)
            save_user(cur, user_id, s)       # 日替わり/週替わりのリセットを保存しておく
        return jsonify(user_view(s))

    def require_phase(*allowed):
        ph = phase()
        if ph not in allowed:
            msg = 'このイベントは終了しました' if ph in ('ended',) else 'まだ開催されていません'
            raise ApiError(msg, 403)

    # --- ゲーム結果の報告 → キャンディ獲得 & ミッション進行 ---
    @app.post('/api/event/halloween/report')
    @guard
    def hw_report():
        user_id = require_auth()
        if phase() != 'active':
            return jsonify({'ok': True, 'active': False})
        b = request.get_json(silent=True) or {}

        def num(key, hi):
            try:
                return max(0, min(hi, int(b.get(key) or 0)))
            except (TypeError, ValueError):
                return 0
        score, lines, tspins, pcs = num('score', 50_000_000), num('lines', 400), num('tspins', 100), num('pcs', 20)
        is_vs, is_duel = bool(b.get('vs')), bool(b.get('duel'))
        is_tetris = bool(b.get('tetris'))
        mode = str(b.get('mode') or '')
        mini_id = mode[5:] if mode.startswith('mini-') and mode[5:] in MINI_IDS else None
        with db_transaction() as cur:
            row = lock_user(cur, user_id)
            if not row:
                raise ApiError('ユーザーが見つかりません', 404)
            s = load_state_from_row(row)
            st = s['st']
            before = {m['id'] for m in _mission_view(daily_missions(), st['daily']) + _mission_view(weekly_missions(), st['weekly']) if m['done']}
            now_ts = time.time()
            gained = 0
            dex_drop, new_stamp = None, False
            mult, boost_label = candy_boost()
            if now_ts - float(st.get('lastReport') or 0) >= 8:       # 連打対策(8秒以内の連続報告は無視)
                base = candies_for_game(score, lines, tspins, pcs, is_duel)
                if mini_id:
                    base += C.MINI_GAME_BONUS
                gained = int(round(base * mult))
                add_candies(st, gained)
                add_stat_both(st, 'games', 1)
                add_stat_both(st, 'score_max', score)
                if mini_id:
                    add_stat_both(st, 'mini_games', 1)
                    add_kind(st['daily'], mini_id)
                    add_kind(st['weekly'], mini_id)
                    new_stamp = stamp_add(st, mini_id)
                if is_tetris:
                    add_stat_both(st, 'lines', lines)
                if tspins:
                    add_stat_both(st, 'tspin', tspins)
                if pcs:
                    add_stat_both(st, 'pc', pcs)
                if is_vs:
                    add_stat_both(st, 'vs_games', 1)
                if is_duel:
                    add_stat_both(st, 'duels', 1)
                if random.random() < C.DEX_DROP_CHANCE:               # 📖 おばけ図鑑に出会いがあるかも
                    dex_drop = dex_give(st)
            st['lastReport'] = now_ts
            save_user(cur, user_id, s)
            after_views = _mission_view(daily_missions(), st['daily']) + _mission_view(weekly_missions(), st['weekly'])
        newly = [m['text'] for m in after_views if m['done'] and m['id'] not in before]
        return jsonify({'ok': True, 'active': True, 'gained': gained, 'candies': st['candies'],
                        'totalCandies': st['totalCandies'], 'newlyCompleted': newly,
                        'multiplier': mult, 'boostLabel': boost_label, 'dex': dex_drop, 'newStamp': new_stamp})

    # --- 毎日のログインボーナス(トリック・オア・トリート) ---
    @app.post('/api/event/halloween/login-bonus')
    @guard
    def hw_login_bonus():
        user_id = require_auth()
        require_phase('active')
        with db_transaction() as cur:
            s = load_state_from_row(lock_user(cur, user_id) or {})
            st = s['st']
            today = _today()
            if st['lastLogin'] == today:
                raise ApiError('今日のボーナスは受け取り済みです', 400)
            yesterday = (now_dt() - timedelta(days=1)).strftime('%Y-%m-%d')
            st['streak'] = st['streak'] + 1 if st['lastLogin'] == yesterday else 1
            st['lastLogin'] = today
            day = ((st['streak'] - 1) % LOGIN_CYCLE) + 1
            r = login_reward(day)
            add_candies(st, r['candies'])
            s['coins'] += r['coins']
            st['tickets'] += r['tickets']
            save_user(cur, user_id, s)
        return jsonify({'ok': True, 'day': day, 'reward': r, 'coins': s['coins'], 'candies': st['candies'],
                        'tickets': st['tickets'], 'streak': st['streak']})

    # --- ミッション報酬 ---
    @app.post('/api/event/halloween/missions/claim')
    @guard
    def hw_missions_claim():
        user_id = require_auth()
        require_phase('active', 'grace')
        b = request.get_json(silent=True) or {}
        with db_transaction() as cur:
            s = load_state_from_row(lock_user(cur, user_id) or {})
            st = s['st']
            targets = []
            for scope, missions in (('daily', daily_missions()), ('weekly', weekly_missions())):
                bucket = st[scope]
                for m in missions:
                    if m['id'] in bucket['claimed'] or _progress(bucket['stats'], m) < m['target']:
                        continue
                    if b.get('all') or (b.get('id') == m['id']):
                        targets.append((bucket, m))
            if not targets:
                raise ApiError('受け取れるミッション報酬がありません', 400)
            total = 0
            for bucket, m in targets:
                bucket['claimed'].append(m['id'])
                total += m['candies']
            add_candies(st, total)
            save_user(cur, user_id, s)
        return jsonify({'ok': True, 'claimedCount': len(targets), 'candies': total, 'total': st['candies']})

    # --- ハロウィンロード報酬 ---
    @app.post('/api/event/halloween/road/claim')
    @guard
    def hw_road_claim():
        user_id = require_auth()
        require_phase('active', 'grace')
        b = request.get_json(silent=True) or {}
        with db_transaction() as cur:
            s = load_state_from_row(lock_user(cur, user_id) or {})
            st = s['st']
            targets = [t for t in ROAD
                       if st['totalCandies'] >= t['need'] and t['tier'] not in st['claimedRoad']
                       and (b.get('all') or b.get('tier') == t['tier'])]
            if not targets:
                raise ApiError('受け取れる報酬がありません', 400)
            gained = []
            for t in targets:
                gained.append(apply_reward(t['reward'], s))
                st['claimedRoad'].append(t['tier'])
            save_user(cur, user_id, s)
        return jsonify({'ok': True, 'claimedCount': len(targets), 'gained': gained, 'coins': s['coins'],
                        'tickets': st['tickets'], 'ownedSkins': s['skins'], 'battlepassXp': s['bpxp']})

    # --- 交換所 ---
    @app.post('/api/event/halloween/shop/buy')
    @guard
    def hw_shop_buy():
        user_id = require_auth()
        require_phase('active', 'grace')
        b = request.get_json(silent=True) or {}
        item = SHOP_BY_ID.get(b.get('id'))
        if not item:
            raise ApiError('商品が見つかりません', 404)
        with db_transaction() as cur:
            s = load_state_from_row(lock_user(cur, user_id) or {})
            st = s['st']
            bought = st['shopBought'].get(item['id'], 0)
            if bought >= item['limit']:
                raise ApiError('交換上限に達しています', 400)
            if st['candies'] < item['cost']:
                raise ApiError('キャンディが足りません', 400)
            st['candies'] -= item['cost']
            st['shopBought'][item['id']] = bought + 1
            text = apply_reward(item['give'], s)
            save_user(cur, user_id, s)
        return jsonify({'ok': True, 'gained': text, 'candies': st['candies'], 'coins': s['coins'],
                        'tickets': st['tickets'], 'ownedSkins': s['skins'], 'battlepassXp': s['bpxp']})

    # --- ハロウィンガチャ(期間限定。終了時刻を過ぎると、フロントが古いままでもサーバーが拒否する) ---
    @app.post('/api/event/halloween/gacha/pull')
    @guard
    def hw_gacha_pull():
        user_id = require_auth()
        b = request.get_json(silent=True) or {}
        try:
            count = int(b.get('count') or 1)
        except (TypeError, ValueError):
            count = 0
        if count not in (1, 10):
            raise ApiError('回数が不正です', 400)
        with db_transaction() as cur:
            if phase() != 'active':
                raise ApiError('ハロウィンガチャの開催は終了しました', 403)
            s = load_state_from_row(lock_user(cur, user_id) or {})
            st = s['st']
            tickets_used = min(st['tickets'], count)
            candies_needed = (count - tickets_used) * GACHA_COST
            if st['candies'] < candies_needed:
                raise ApiError('キャンディが足りません', 400)
            st['candies'] -= candies_needed      # ※消費しても、ロードの進行(累計 totalCandies)は減らない
            st['tickets'] -= tickets_used
            results = []
            for i in range(count):
                st['pity'] += 1
                st['pulls'] += 1
                roll = random.random()
                natural = roll < GACHA_RATE_PER_SKIN * len(GACHA_POOL)
                pity_win = (not natural) and st['pity'] >= GACHA_PITY
                r = {'usedTicket': i < tickets_used, 'pityTriggered': False, 'skinId': None,
                     'coinsGained': 0, 'candiesGained': 0}
                if natural or pity_win:
                    unowned = [x for x in GACHA_POOL if x not in s['skins']]
                    sid = random.choice(unowned) if (pity_win and unowned) else random.choice(GACHA_POOL)
                    st['pity'] = 0
                    r['skinId'], r['pityTriggered'] = sid, pity_win
                    if sid in s['skins']:
                        r.update(resultType='duplicate', candiesGained=GACHA_DUP_CANDIES)
                    else:
                        s['skins'].append(sid)
                        r['resultType'] = 'win'
                else:
                    r.update(resultType='miss', candiesGained=GACHA_MISS_CANDIES)
                add_candies(st, r['candiesGained'])
                results.append(r)
            add_stat_both(st, 'hw_pulls', count)
            save_user(cur, user_id, s)
        return jsonify({'ok': True, 'count': count, 'results': results, 'spentCandies': candies_needed,
                        'ticketsUsed': tickets_used, 'coins': s['coins'], 'candies': st['candies'],
                        'tickets': st['tickets'], 'ownedSkins': s['skins'],
                        'pity': st['pity'], 'pityThreshold': GACHA_PITY, 'pulls': st['pulls']})

    # --- キャンディランキング ---
    @app.get('/api/event/halloween/ranking')
    @guard
    def hw_ranking():
        user_id = require_auth()
        rows = db_query(
            "SELECT id, (event_state->>'totalCandies')::bigint AS total FROM users "
            "WHERE event_state->>'id' = %s AND COALESCE((event_state->>'totalCandies')::bigint, 0) > 0 "
            "ORDER BY total DESC, id ASC LIMIT 20", [EVENT_ID])
        mine = db_query("SELECT (event_state->>'totalCandies')::bigint AS total FROM users "
                        "WHERE id = %s AND event_state->>'id' = %s", [user_id, EVENT_ID])
        my_total = int((mine[0]['total'] if mine else 0) or 0)
        my_rank = None
        if my_total > 0:
            r = db_query("SELECT COUNT(*) AS n FROM users WHERE event_state->>'id' = %s "
                         "AND (event_state->>'totalCandies')::bigint > %s", [EVENT_ID, my_total])
            my_rank = int(r[0]['n']) + 1
        return jsonify({'ranking': [{'id': r['id'], 'total': int(r['total'])} for r in rows],
                        'me': {'id': user_id, 'total': my_total, 'rank': my_rank}})


    # ================= v3.6 パート2 のAPI =================
    def with_state(user_id, fn, phases=('active',)):
        """ユーザー行をロックして状態を読み、fn(s) を実行して保存する。fn の戻り値を返す"""
        require_phase(*phases)
        with db_transaction() as cur:
            row = lock_user(cur, user_id)
            if not row:
                raise ApiError('ユーザーが見つかりません', 404)
            s = load_state_from_row(row)
            out = fn(s)
            save_user(cur, user_id, s)
        return out

    def body_int(key, default=0):
        b = request.get_json(silent=True) or {}
        try:
            return int(b.get(key, default))
        except (TypeError, ValueError):
            return default

    # --- 🚪 トリック・オア・トリート(3つのドア) ---
    @app.post('/api/event/halloween/tot/open')
    @guard
    def hw_tot_open():
        user_id = require_auth()
        door = max(0, min(2, body_int('door', 0)))

        def fn(s):
            st = s['st']
            tot = st['tot']
            if tot['used'] >= C.TOT_MAX_PER_DAY:
                raise ApiError('今日のドアはもう開けられません。また明日！', 400)
            cost = 0 if tot['used'] < C.TOT_FREE_PER_DAY else C.TOT_EXTRA_COST
            if st['candies'] < cost:
                raise ApiError('キャンディが足りません(2回目からは🍬%d必要です)' % C.TOT_EXTRA_COST, 400)
            st['candies'] -= cost
            tot['used'] += 1
            st['totTotal'] += 1
            add_stat_both(st, 'tot', 1)
            res = roll_tot()
            g = res['give']
            if g.get('candies'):
                add_candies(st, g['candies'])
            if g.get('coins'):
                s['coins'] += g['coins']
            if g.get('tickets'):
                st['tickets'] += g['tickets']
            res['dex'] = dex_give(st, rare_only=True) if res['kind'] == 'dex' else None
            others = [roll_tot() for _ in range(2)]
            doors = [{'kind': o['kind'], 'title': o['title']} for o in others]
            doors.insert(door, {'kind': res['kind'], 'title': res['title']})
            return {'ok': True, 'door': door, 'doors': doors, 'result': res, 'cost': cost, **wallet(s)}
        return jsonify(with_state(user_id, fn))

    # --- 🔮 きょうのおみくじ ---
    @app.post('/api/event/halloween/fortune')
    @guard
    def hw_fortune():
        user_id = require_auth()

        def fn(s):
            st = s['st']
            today = _today()
            if isinstance(st.get('fortune'), dict) and st['fortune'].get('date') == today:
                raise ApiError('おみくじは1日1回です。また明日！', 400)
            f = fortune_roll(user_id, today)
            st['fortune'] = f
            add_candies(st, f['candies'])
            add_stat_both(st, 'fortune', 1)
            return {'ok': True, 'fortune': f, **wallet(s)}
        return jsonify(with_state(user_id, fn))

    # --- 🌱 かぼちゃ畑 ---
    @app.post('/api/event/halloween/patch/plant')
    @guard
    def hw_patch_plant():
        user_id = require_auth()
        b = request.get_json(silent=True) or {}
        kind = C.PATCH_BY_ID.get(b.get('kind'))
        idx = body_int('plot', -1)
        if not kind:
            raise ApiError('育てるかぼちゃを選んでください', 400)

        def fn(s):
            st = s['st']
            if idx < 0 or idx >= C.PATCH_PLOTS:
                raise ApiError('畑が見つかりません', 400)
            if st['patch'][idx]:
                raise ApiError('ここにはもう植えてあります', 400)
            st['patch'][idx] = {'kind': kind[0], 'at': time.time()}
            return {'ok': True, 'patch': patch_view(st), **wallet(s)}
        return jsonify(with_state(user_id, fn))

    @app.post('/api/event/halloween/patch/harvest')
    @guard
    def hw_patch_harvest():
        user_id = require_auth()
        b = request.get_json(silent=True) or {}
        want_all = bool(b.get('all'))
        idx = body_int('plot', -1)

        def fn(s):
            st = s['st']
            now_ts = time.time()
            got, n = 0, 0
            mult, _ = candy_boost()
            for i, p in enumerate(st['patch']):
                if not p or p.get('kind') not in C.PATCH_BY_ID or not (want_all or i == idx):
                    continue
                k = C.PATCH_BY_ID[p['kind']]
                if now_ts < float(p.get('at') or 0) + k[3]:
                    continue
                add_candies(st, int(round(k[4] * mult)))
                got += int(round(k[4] * mult))
                n += 1
                st['patch'][i] = None
            if not n:
                raise ApiError('まだ収穫できるかぼちゃがありません', 400)
            st['harvestTotal'] += n
            add_stat_both(st, 'harvest', n)
            return {'ok': True, 'harvested': n, 'gained': got, 'patch': patch_view(st), **wallet(s)}
        return jsonify(with_state(user_id, fn, ('active', 'grace')))

    # --- 👻 さまようおばけ ---
    @app.post('/api/event/halloween/ghost/catch')
    @guard
    def hw_ghost_catch():
        user_id = require_auth()

        def fn(s):
            st = s['st']
            g = st['ghost']
            now_ts = time.time()
            if g['count'] >= C.GHOST_MAX_PER_DAY:
                raise ApiError('今日のおばけは、もうみんな逃げちゃった。また明日！', 400)
            if now_ts - float(g.get('last') or 0) < C.GHOST_COOLDOWN_SEC:
                raise ApiError('おばけは、まだ隠れています…', 400)
            gold = random.random() < C.GHOST_GOLD_CHANCE
            mult, _ = candy_boost()
            n = int(round((C.GHOST_GOLD_CANDIES if gold else random.randint(*C.GHOST_NORMAL)) * mult))
            g['count'] += 1
            g['last'] = now_ts
            st['ghostTotal'] += 1
            add_candies(st, n)
            add_stat_both(st, 'ghost', 1)
            return {'ok': True, 'gained': n, 'gold': gold, 'left': C.GHOST_MAX_PER_DAY - g['count'], **wallet(s)}
        return jsonify(with_state(user_id, fn))

    # --- 📖 図鑑のごほうび ---
    @app.post('/api/event/halloween/dex/claim')
    @guard
    def hw_dex_claim():
        user_id = require_auth()
        b = request.get_json(silent=True) or {}
        want_all, need = bool(b.get('all')), body_int('need', -1)

        def fn(s):
            st = s['st']
            n = len(st['dex'])
            targets = [r for r in C.DEX_REWARDS if n >= r['need'] and r['need'] not in st['dexClaimed'] and (want_all or r['need'] == need)]
            if not targets:
                raise ApiError('受け取れる報酬がありません', 400)
            gained = []
            for r in targets:
                gained.append(apply_reward(r['reward'], s))
                st['dexClaimed'].append(r['need'])
            return {'ok': True, 'claimedCount': len(targets), 'rewards': [r['reward'] for r in targets], **wallet(s)}
        return jsonify(with_state(user_id, fn, ('active', 'grace')))

    # --- 🎪 スタンプラリーのごほうび ---
    @app.post('/api/event/halloween/stamps/claim')
    @guard
    def hw_stamps_claim():
        user_id = require_auth()
        b = request.get_json(silent=True) or {}
        want_all, key = bool(b.get('all')), str(b.get('key') or '')

        def fn(s):
            st = s['st']
            n = len(st['stamps'])
            targets = [(k, r) for k, need, r in stamp_steps() if n >= need and k not in st['stampClaimed'] and (want_all or k == key)]
            if not targets:
                raise ApiError('受け取れる報酬がありません', 400)
            for k, r in targets:
                apply_reward(r, s)
                st['stampClaimed'].append(k)
            return {'ok': True, 'claimedCount': len(targets), 'rewards': [r for _, r in targets], **wallet(s)}
        return jsonify(with_state(user_id, fn, ('active', 'grace')))

    # --- 🌍 みんなでキャンディ大作戦(全員の累計の合計) ---
    comm_cache = {'t': 0.0, 'v': 0}

    def community_total():
        if time.time() - comm_cache['t'] < C.COMMUNITY_CACHE_SEC:
            return comm_cache['v']
        rows = db_query("SELECT COALESCE(SUM((event_state->>'totalCandies')::bigint), 0) AS n FROM users "
                        "WHERE event_state->>'id' = %s", [EVENT_ID])
        comm_cache['v'] = int(rows[0]['n'] or 0) if rows else 0
        comm_cache['t'] = time.time()
        return comm_cache['v']

    @app.get('/api/event/halloween/community')
    @guard
    def hw_community():
        user_id = require_auth()
        total = community_total()
        row = db_query('SELECT event_state FROM users WHERE id = %s', [user_id])
        st = norm_state(row[0].get('event_state') if row else None)
        goals = [{'need': g['need'], 'rewards': g['rewards'], 'reached': total >= g['need'], 'claimed': g['need'] in st['communityClaimed']}
                 for g in C.COMMUNITY_GOALS]
        return jsonify({'total': total, 'goals': goals})

    @app.post('/api/event/halloween/community/claim')
    @guard
    def hw_community_claim():
        user_id = require_auth()
        total = community_total()

        def fn(s):
            st = s['st']
            targets = [g for g in C.COMMUNITY_GOALS if total >= g['need'] and g['need'] not in st['communityClaimed']]
            if not targets:
                raise ApiError('受け取れる報酬がありません', 400)
            rewards = []
            for g in targets:
                for r in g['rewards']:
                    apply_reward(r, s)
                    rewards.append(r)
                st['communityClaimed'].append(g['need'])
            return {'ok': True, 'claimedCount': len(targets), 'rewards': rewards, **wallet(s)}
        return jsonify(with_state(user_id, fn, ('active', 'grace')))

    # --- 👤 ゲストでためたキャンディの引き継ぎ(アカウントを作ったあと、1回だけ) ---
    @app.post('/api/event/halloween/guest-carryover')
    @guard
    def hw_guest_carryover():
        user_id = require_auth()
        n = max(0, min(C.GUEST_CARRYOVER_MAX, body_int('candies', 0)))

        def fn(s):
            st = s['st']
            if st['guestBonus']:
                raise ApiError('引き継ぎは1回だけです', 400)
            if n <= 0:
                return {'ok': True, 'gained': 0, **wallet(s)}
            st['guestBonus'] = True
            add_candies(st, n)
            return {'ok': True, 'gained': n, **wallet(s)}
        return jsonify(with_state(user_id, fn, ('active', 'grace')))
