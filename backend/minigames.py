# -*- coding: utf-8 -*-
"""
🕹 ミニゲームのランキング

  POST /api/mini/score    {game, score}        自己ベストを記録(ログイン必須)
  GET  /api/mini/ranking?game=g2048            上位20人 + 自分の順位
  GET  /api/mini/summary                       全ゲームの「自分の順位」一覧(メニュー表示用)

* 「高いほど良い」ゲームと「低いほど良い」ゲームがあり、ここで決める(クライアントの自己申告は信用しない)
* ありえない値は無効として捨てる(簡易的な不正対策)。クライアントがコイン等を自己申告する作りなので、厳密な対策ではない
"""
from flask import request, jsonify

# game: (better, min, max, 単位)
#   better='high' → 大きいほど上位 / 'low' → 小さいほど上位
GAMES = {
    'g2048':  ('high', 4,  2_000_000, '点'),    # 2048(スコア)
    'snake':  ('high', 0,  220,       '個'),    # 15×15マスなので最大でも220個
    'memory': ('low',  8,  999,       '手'),    # 8組なので最短8手
    'whack':  ('high', 0,  1200,      '点'),    # 30秒
    'mines':  ('low',  4,  3599,      '秒'),    # 勝ったときのクリア秒数
    'flight': ('high', 0,  5000,      '枚'),
    # --- v3.6 で追加 ---
    'dino':     ('high', 0,  99999, '点'),     # ゴースト・ラン(走った距離)
    'breakout': ('high', 0,  20000, '点'),     # かぼちゃブロック崩し
    'simon':    ('high', 0,  99,    'ラウンド'),  # ろうそくシモン
    'reflex':   ('low',  80, 2000,  'ms'),     # ガイコツ反射神経(5回の平均)
    'catch':    ('high', 0,  2000,  '点'),     # キャンディキャッチ
    'math':     ('high', 0,  150,   '問'),     # ばけばけ計算
    'maze':     ('low',  3,  3599,  '秒'),     # まよいの洋館
    'slide':    ('low',  8,  999,   '手'),     # スライドパズル
    'lights':   ('low',  3,  999,   '手'),     # ともしびライツアウト
    'numtouch': ('low',  30, 6000,  '0.1秒'),  # ナンバータッチ(0.1秒単位)
    'stroop':   ('high', 0,  150,   '問'),     # いろいろモンスター
    'stack':    ('high', 0,  400,   '段'),     # ホーンテッド・スタック
}


def validate(game, score):
    """(ok, score:int) を返す。範囲外や不明なゲームは ok=False"""
    spec = GAMES.get(game)
    if not spec:
        return False, 0
    try:
        s = int(score)
    except (TypeError, ValueError):
        return False, 0
    _, lo, hi, _ = spec
    if s < lo or s > hi:
        return False, 0
    return True, s


# ---------- DB(テストで差し替えやすいよう分離) ----------
def db_upsert(db_query, game, user, score, better):
    fn = 'GREATEST' if better == 'high' else 'LEAST'
    rows = db_query(
        f"INSERT INTO mini_scores (game, user_id, best, plays) VALUES (%s, %s, %s, 1) "
        f"ON CONFLICT (game, user_id) DO UPDATE SET best = {fn}(mini_scores.best, EXCLUDED.best), "
        f"plays = mini_scores.plays + 1, updated_at = NOW() RETURNING best", [game, user, score])
    return int(rows[0]['best']) if rows else score


def db_top(db_query, game, better, limit=20):
    order = 'DESC' if better == 'high' else 'ASC'
    rows = db_query(f"SELECT user_id, best FROM mini_scores WHERE game = %s ORDER BY best {order}, updated_at ASC LIMIT %s", [game, limit])
    return [{'id': r['user_id'], 'score': int(r['best'])} for r in rows]


def db_mine(db_query, game, user, better):
    """(自分のベスト or None, 順位 or None)"""
    mine = db_query("SELECT best FROM mini_scores WHERE game = %s AND user_id = %s", [game, user])
    if not mine:
        return None, None
    best = int(mine[0]['best'])
    cmp_ = '>' if better == 'high' else '<'
    n = db_query(f"SELECT COUNT(*) AS n FROM mini_scores WHERE game = %s AND best {cmp_} %s", [game, best])
    return best, int(n[0]['n']) + 1


def register(app, db_query, require_auth, ApiError):
    def fail(e):
        return jsonify({'error': e.message}), e.status

    @app.post('/api/mini/score')
    def mini_score():
        try:
            user = require_auth()
            b = request.get_json(silent=True) or {}
            game = b.get('game')
            ok, score = validate(game, b.get('score'))
            if not ok:
                return jsonify({'ok': False, 'ignored': True})       # 範囲外は静かに無視(エラーにはしない)
            better = GAMES[game][0]
            best = db_upsert(db_query, game, user, score, better)
            _, rank = db_mine(db_query, game, user, better)
            return jsonify({'ok': True, 'best': best, 'rank': rank})
        except ApiError as e:
            return fail(e)
        except Exception as err:
            print(f'mini score error: {err}')
            return jsonify({'error': 'サーバーエラー'}), 500

    @app.get('/api/mini/ranking')
    def mini_ranking():
        try:
            game = request.args.get('game')
            if game not in GAMES:
                raise ApiError('ゲームが不明です', 400)
            better, _, _, unit = GAMES[game]
            top = db_top(db_query, game, better)
            me = None
            try:
                user = require_auth()
                best, rank = db_mine(db_query, game, user, better)
                me = {'id': user, 'best': best, 'rank': rank}
            except ApiError:
                pass                                                   # 未ログインでも一覧は見られる
            return jsonify({'game': game, 'better': better, 'unit': unit, 'ranking': top, 'me': me})
        except ApiError as e:
            return fail(e)
        except Exception as err:
            print(f'mini ranking error: {err}')
            return jsonify({'error': 'サーバーエラー'}), 500

    @app.get('/api/mini/summary')
    def mini_summary():
        try:
            user = require_auth()
            out = {}
            for game, (better, _, _, unit) in GAMES.items():
                best, rank = db_mine(db_query, game, user, better)
                out[game] = {'best': best, 'rank': rank, 'unit': unit}
            return jsonify({'games': out})
        except ApiError as e:
            return fail(e)
        except Exception as err:
            print(f'mini summary error: {err}')
            return jsonify({'error': 'サーバーエラー'}), 500
