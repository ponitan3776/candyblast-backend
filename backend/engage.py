# -*- coding: utf-8 -*-
"""
🎁 毎日ログインボーナス(1年中)・ウェルカムギフト・ゲスト向け情報   (v3.6)

  GET  /api/bonus/daily            今日の状況(連続日数・7日サイクル・受け取り済みか)   ※ログイン必須
  POST /api/bonus/daily/claim      今日のログインボーナスを受け取る(コイン・バトルパスXP)
  GET  /api/guest/perks            ゲスト向け「アカウントを作ると…」の一覧(ログイン不要。画面の文章をサーバーで一元管理)

* ハロウィンのログボ(キャンディ)とは別で、イベントが終わっても続く「毎日のログインボーナス」です。
* ウェルカムギフト: 新規登録の直後に、ギフト(🎁)としてプレゼントを1回だけ届けます(register から send_welcome_gift を呼ぶ)。
* 数字は先頭の定数で調整できます。
"""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import jsonify

JST = ZoneInfo('Asia/Tokyo')
CYCLE = 7

# 1〜7日目(7日目でひと回り。続けるほど、後半が豪華)
DAILY_REWARDS = [
    {'coins': 300,  'bpxp': 0},
    {'coins': 400,  'bpxp': 300},
    {'coins': 500,  'bpxp': 0},
    {'coins': 700,  'bpxp': 500},
    {'coins': 900,  'bpxp': 0},
    {'coins': 1200, 'bpxp': 800},
    {'coins': 3000, 'bpxp': 2000},
]
# 連続日数のおまけ(ちょうどその日に1回だけ上乗せ)
STREAK_BONUS = {3: {'coins': 1000}, 14: {'coins': 5000, 'bpxp': 3000}, 30: {'coins': 20000, 'bpxp': 10000}, 60: {'coins': 40000}, 100: {'coins': 100000, 'bpxp': 30000}}

WELCOME_GIFT = {'coins': 3000, 'candies': 100, 'bp_xp': 2000, 'event_xp': 0,
                'message': 'ようこそ CandyBlast へ！🎃 アカウント作成ありがとう。ウェルカムギフトです。毎日ログインすると、もっといいものがもらえるよ！'}

# ゲストに見せる「アカウントを作るメリット」(画面の通知や一覧に使う)
GUEST_PERKS = [
    {'icon': '🎁', 'title': 'ウェルカムギフト', 'text': '作った瞬間に🪙3,000・🍬100・バトルパスXPがもらえる'},
    {'icon': '📆', 'title': '毎日ログインボーナス', 'text': '毎日もらえて、続けるほど豪華(7・14・30日…で特別ボーナス)'},
    {'icon': '🎃', 'title': 'ハロウィンイベントに参加', 'text': 'キャンディ集め・ガチャ・ロード・図鑑・畑・スタンプラリーが全部遊べる'},
    {'icon': '🏆', 'title': 'ランキングに載る', 'text': 'ミニゲームの記録が全国ランキングに載る。名前も残る'},
    {'icon': '☁️', 'title': 'データが消えない', 'text': 'コイン・スキン・記録がサーバーに保存。機種変更しても引き継げる'},
    {'icon': '👥', 'title': 'フレンド・チャット・対決', 'text': 'みんなとチャットしたり、フレンド対決ができる'},
    {'icon': '🏷', 'title': '称号・ギフト・投票', 'text': '称号を集めたり、運営からのプレゼントを受け取れる'},
]

INIT_SQL = """
CREATE TABLE IF NOT EXISTS daily_bonus (
    user_id TEXT PRIMARY KEY, streak INTEGER NOT NULL DEFAULT 0, best_streak INTEGER NOT NULL DEFAULT 0,
    last_date TEXT, total_days INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMP DEFAULT NOW()
);
ALTER TABLE daily_bonus ENABLE ROW LEVEL SECURITY;
"""


def init_tables(db_execute):
    db_execute(INIT_SQL)


def today_jst():
    return datetime.now(JST).strftime('%Y-%m-%d')


def yesterday_jst():
    return (datetime.now(JST) - timedelta(days=1)).strftime('%Y-%m-%d')


def day_in_cycle(streak):
    return ((max(1, streak) - 1) % CYCLE) + 1


def view(row):
    """DBの1行(なければ None) → 画面に出す状況"""
    today, yest = today_jst(), yesterday_jst()
    last = row.get('last_date') if row else None
    streak = int(row.get('streak') or 0) if row else 0
    claimed = last == today
    next_streak = streak if claimed else (streak + 1 if last == yest else 1)
    return {
        'claimedToday': claimed, 'streak': streak if (claimed or last == yest) else 0,
        'nextStreak': next_streak, 'day': day_in_cycle(next_streak if not claimed else streak),
        'cycle': CYCLE, 'rewards': DAILY_REWARDS, 'streakBonus': {str(k): v for k, v in STREAK_BONUS.items()},
        'bestStreak': int(row.get('best_streak') or 0) if row else 0,
        'totalDays': int(row.get('total_days') or 0) if row else 0,
    }


def send_welcome_gift(db_query, user_id):
    """新規登録の直後に呼ぶ。失敗しても登録は止めない"""
    try:
        g = WELCOME_GIFT
        db_query("INSERT INTO gifts (batch_id, to_user, from_user, message, coins, candies, bp_xp, event_xp) "
                 "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                 ['welcome-' + user_id, user_id, 'spirit', g['message'], g['coins'], g['candies'], g['bp_xp'], g['event_xp']])
    except Exception as err:
        print(f'welcome gift error: {err}')


def register(app, db_query, db_transaction, require_auth, ApiError):
    def fail(e):
        return jsonify({'error': e.message}), e.status

    @app.get('/api/bonus/daily')
    def bonus_status():
        try:
            user = require_auth()
            rows = db_query('SELECT streak, best_streak, last_date, total_days FROM daily_bonus WHERE user_id = %s', [user])
            return jsonify(view(rows[0] if rows else None))
        except ApiError as e:
            return fail(e)
        except Exception as err:
            print(f'bonus status error: {err}')
            return jsonify({'error': 'サーバーエラー'}), 500

    @app.post('/api/bonus/daily/claim')
    def bonus_claim():
        try:
            user = require_auth()
            today = today_jst()
            with db_transaction() as cur:
                cur.execute('SELECT streak, best_streak, last_date, total_days FROM daily_bonus WHERE user_id = %s FOR UPDATE', [user])
                row = cur.fetchone()
                if row and row['last_date'] == today:
                    raise ApiError('今日のボーナスは受け取り済みです', 400)
                streak = (int(row['streak']) + 1) if (row and row['last_date'] == yesterday_jst()) else 1
                best = max(streak, int(row['best_streak']) if row else 0)
                total = (int(row['total_days']) if row else 0) + 1
                day = day_in_cycle(streak)
                reward = dict(DAILY_REWARDS[day - 1])
                extra = STREAK_BONUS.get(streak)
                if extra:
                    reward['coins'] = reward.get('coins', 0) + extra.get('coins', 0)
                    reward['bpxp'] = reward.get('bpxp', 0) + extra.get('bpxp', 0)
                cur.execute("INSERT INTO daily_bonus (user_id, streak, best_streak, last_date, total_days) VALUES (%s, %s, %s, %s, %s) "
                            "ON CONFLICT (user_id) DO UPDATE SET streak = EXCLUDED.streak, best_streak = EXCLUDED.best_streak, "
                            "last_date = EXCLUDED.last_date, total_days = EXCLUDED.total_days, updated_at = NOW()",
                            [user, streak, best, today, total])
                cur.execute('UPDATE users SET coins = COALESCE(coins, 0) + %s, battlepass_xp = COALESCE(battlepass_xp, 0) + %s '
                            'WHERE id = %s RETURNING coins, battlepass_xp', [reward.get('coins', 0), reward.get('bpxp', 0), user])
                u = cur.fetchone()
            return jsonify({'ok': True, 'day': day, 'streak': streak, 'reward': reward, 'bonusHit': bool(extra),
                            'coins': int(u['coins']), 'battlepassXp': int(u['battlepass_xp'])})
        except ApiError as e:
            return fail(e)
        except Exception as err:
            print(f'bonus claim error: {err}')
            return jsonify({'error': 'サーバーエラー'}), 500

    @app.get('/api/guest/perks')
    def guest_perks():
        return jsonify({'perks': GUEST_PERKS, 'welcome': {k: WELCOME_GIFT[k] for k in ('coins', 'candies', 'bp_xp')},
                        'daily': DAILY_REWARDS})
