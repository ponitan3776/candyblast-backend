# -*- coding: utf-8 -*-
"""
💬🗳🎁 ソーシャル機能

  🗳 投票(ゲスト以外が投票できる。管理者が 作成/編集/終了/リセット/削除 と、誰が何に投票したかの確認)
     GET  /api/polls                        一覧(結果つき。ゲストは閲覧のみ)
     POST /api/polls/<id>/vote {option}     投票(やり直し可・終了後は不可)
     POST /api/admin/poll/create|update|close|reset|delete
     GET  /api/admin/poll/<id>/voters       管理者だけ: 誰がどれに投票したか

  🎁 ギフト(管理者が メッセージ + コイン/イベント通貨/XP/イベントXP を贈る)
     POST /api/admin/gift {to, message, coins, candies, bpXp, eventXp}     to = ユーザーID or 'all'
     GET  /api/admin/gifts   /  POST /api/admin/gift/cancel {batch}
     GET  /api/gifts/pending                受け取り待ち一覧(ログイン時・ゲームオーバー後にクライアントが確認)
     POST /api/gifts/claim {id}             受け取り(サーバーが報酬を付与)

  ✏️ メッセージの編集・送信取り消し(サーバーチャット / DM。自分のメッセージのみ)
     POST /api/chat/edit|retract   /   POST /api/dm/edit|retract

  👤 GET /api/admin/users?q=               管理者パネルの「ユーザーを選ぶ」用
"""
import json
import re
import uuid
from flask import request, jsonify

import halloween as H

POLL_MIN_OPTIONS, POLL_MAX_OPTIONS = 2, 8
POLL_Q_MAX, POLL_OPT_MAX = 120, 40
GIFT_MAX = 10 ** 12
GIFT_MSG_MAX = 300
CHAT_MAX, DM_MAX = 200, 300

INIT_SQL = """
CREATE TABLE IF NOT EXISTS polls (
    id SERIAL PRIMARY KEY, question TEXT NOT NULL, options JSONB NOT NULL,
    closed BOOLEAN DEFAULT FALSE, created_by TEXT, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE, user_id TEXT NOT NULL,
    option_idx INTEGER NOT NULL, voted_at TIMESTAMP DEFAULT NOW(), PRIMARY KEY (poll_id, user_id)
);
CREATE TABLE IF NOT EXISTS gifts (
    id SERIAL PRIMARY KEY, batch_id TEXT NOT NULL, to_user TEXT NOT NULL, from_user TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '', coins BIGINT DEFAULT 0, candies BIGINT DEFAULT 0,
    bp_xp BIGINT DEFAULT 0, event_xp BIGINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(), claimed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS gifts_to_user_idx ON gifts (to_user, claimed_at);
CREATE INDEX IF NOT EXISTS gifts_batch_idx ON gifts (batch_id);
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE dm_messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP;
ALTER TABLE dm_messages ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE polls ENABLE ROW LEVEL SECURITY;
ALTER TABLE poll_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE gifts ENABLE ROW LEVEL SECURITY;
"""


def init_tables(db_execute):
    db_execute(INIT_SQL)


# ===================== 検証ヘルパー(テストしやすいよう外に出す) =====================
def clean_options(options):
    """投票の選択肢を整える。空欄を除き、2〜8個・各40文字まで。不正なら ValueError"""
    if not isinstance(options, list):
        raise ValueError('選択肢が不正です')
    out = [str(o).strip() for o in options if str(o).strip()]
    if len(out) < POLL_MIN_OPTIONS:
        raise ValueError(f'選択肢は{POLL_MIN_OPTIONS}個以上必要です')
    if len(out) > POLL_MAX_OPTIONS:
        raise ValueError(f'選択肢は{POLL_MAX_OPTIONS}個までです')
    if any(len(o) > POLL_OPT_MAX for o in out):
        raise ValueError(f'選択肢は1つ{POLL_OPT_MAX}文字以内にしてください')
    if len(set(out)) != len(out):
        raise ValueError('同じ選択肢が重複しています')
    return out


def clean_question(q):
    q = (q or '').strip()
    if not q:
        raise ValueError('質問を入力してください')
    if len(q) > POLL_Q_MAX:
        raise ValueError(f'質問は{POLL_Q_MAX}文字以内にしてください')
    return q


def clean_amount(v, name):
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        raise ValueError(f'{name}は数字で指定してください')
    if n < 0 or n > GIFT_MAX:
        raise ValueError(f'{name}は0〜{GIFT_MAX:,}の範囲で指定してください')
    return n


def poll_view(row, counts, my_vote):
    opts = row['options'] if isinstance(row['options'], list) else json.loads(row['options'])
    options = [{'text': t, 'votes': int(counts.get(i, 0))} for i, t in enumerate(opts)]
    ca = row.get('created_at')
    return {'id': row['id'], 'question': row['question'], 'options': options,
            'total': sum(o['votes'] for o in options), 'closed': bool(row.get('closed')),
            'myVote': my_vote, 'createdAt': ca.isoformat() if hasattr(ca, 'isoformat') else None}


def gift_items(row):
    return {'coins': int(row.get('coins') or 0), 'candies': int(row.get('candies') or 0),
            'bpXp': int(row.get('bp_xp') or 0), 'eventXp': int(row.get('event_xp') or 0)}


def create_gift(db_query, admin_id, to, message, coins=0, candies=0, bp_xp=0, event_xp=0):
    """ギフトを作る。to='all' なら全ユーザーへ。作った件数と batch を返す。不正なら ValueError"""
    message = (message or '').strip()
    if len(message) > GIFT_MSG_MAX:
        raise ValueError(f'メッセージは{GIFT_MSG_MAX}文字以内にしてください')
    coins, candies = clean_amount(coins, 'コイン'), clean_amount(candies, 'イベント通貨')
    bp_xp, event_xp = clean_amount(bp_xp, 'XP'), clean_amount(event_xp, 'イベントXP')
    if not (coins or candies or bp_xp or event_xp):
        raise ValueError('贈るアイテムを1つ以上指定してください')
    if not message:
        raise ValueError('メッセージを入力してください')
    batch = uuid.uuid4().hex[:12]
    if to == 'all':
        rows = db_query("INSERT INTO gifts (batch_id, to_user, from_user, message, coins, candies, bp_xp, event_xp) "
                        "SELECT %s, id, %s, %s, %s, %s, %s, %s FROM users WHERE COALESCE(banned, FALSE) = FALSE RETURNING id",
                        [batch, admin_id, message, coins, candies, bp_xp, event_xp])
        return len(rows), batch
    if not to or not db_query('SELECT id FROM users WHERE id = %s', [to]):
        raise ValueError(f'ユーザー {to} は見つかりません')
    db_query("INSERT INTO gifts (batch_id, to_user, from_user, message, coins, candies, bp_xp, event_xp) "
             "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
             [batch, to, admin_id, message, coins, candies, bp_xp, event_xp])
    return 1, batch


def apply_gift(s, gift):
    """受け取り処理(メモリ上の state s を書き換える)。H.load_state_from_row の戻り値を受ける"""
    it = gift_items(gift)
    s['coins'] += it['coins']
    s['bpxp'] += it['bpXp']
    H.add_candies(s['st'], it['candies'])                 # 使えるキャンディ + 累計
    s['st']['totalCandies'] += it['eventXp']              # イベントXP = ロードの進行だけ進める
    return it


# ===================== ルート登録 =====================
def register(app, db_query, db_transaction, require_auth, optional_user, ApiError, admin_id, on_chat_change=None):

    def fail(e):
        return jsonify({'error': e.message}), e.status

    def guard(fn):
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except ApiError as e:
                return fail(e)
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            except Exception as err:
                print(f'social error ({fn.__name__}): {err}')
                return jsonify({'error': 'サーバーエラー'}), 500
        wrapper.__name__ = fn.__name__
        return wrapper

    def require_admin():
        uid = require_auth()
        if uid != admin_id:
            raise ApiError('管理者権限がありません', 403)
        return uid

    # ---------------------------------------------------------------- 投票
    POLLS_SQL = """
        SELECT p.id, p.question, p.options, p.closed, p.created_at,
               COALESCE((SELECT json_object_agg(c.option_idx, c.n) FROM
                         (SELECT option_idx, COUNT(*) AS n FROM poll_votes WHERE poll_id = p.id GROUP BY option_idx) c), '{}'::json) AS counts,
               (SELECT option_idx FROM poll_votes WHERE poll_id = p.id AND user_id = %s) AS mine
        FROM polls p {where} ORDER BY p.closed ASC, p.id DESC LIMIT 30
    """

    def load_polls(user, only_id=None):
        # 以前は「質問」「票の数」「自分の票」を3回に分けて取っていた。1回の問い合わせにまとめて速くした
        me = user if (user and user != 'ゲスト') else None
        sql = POLLS_SQL.replace('{where}', 'WHERE p.id = %s' if only_id else '')   # ※ .format は SQL 内の '{}' と衝突するので使わない
        rows = db_query(sql, [me, only_id] if only_id else [me])
        out = []
        for r in rows:
            counts = {int(k): int(v) for k, v in (r.get('counts') or {}).items()}
            out.append(poll_view(r, counts, r.get('mine')))
        return out

    @app.get('/api/polls')
    @guard
    def polls_list():
        return jsonify({'polls': load_polls(optional_user())})

    @app.post('/api/polls/<int:pid>/vote')
    @guard
    def polls_vote(pid):
        user = require_auth()                                   # ゲストは401
        b = request.get_json(silent=True) or {}
        rows = db_query('SELECT id, question, options, closed, created_at FROM polls WHERE id = %s', [pid])
        if not rows:
            raise ApiError('この投票は見つかりません', 404)
        if rows[0].get('closed'):
            raise ApiError('この投票は終了しています', 400)
        n = len(rows[0]['options'] if isinstance(rows[0]['options'], list) else json.loads(rows[0]['options']))
        try:
            idx = int(b.get('option'))
        except (TypeError, ValueError):
            raise ApiError('選択肢が不正です', 400)
        if idx < 0 or idx >= n:
            raise ApiError('選択肢が不正です', 400)
        db_query('INSERT INTO poll_votes (poll_id, user_id, option_idx) VALUES (%s, %s, %s) '
                 'ON CONFLICT (poll_id, user_id) DO UPDATE SET option_idx = EXCLUDED.option_idx, voted_at = NOW() RETURNING poll_id',
                 [pid, user, idx])
        return jsonify({'ok': True, 'poll': load_polls(user, pid)[0]})

    @app.get('/api/admin/poll/<int:pid>/voters')
    @guard
    def poll_voters(pid):
        """🔍 管理者だけ: どのアカウントがどの選択肢に投票したか(投票した順)"""
        require_admin()
        rows = db_query('SELECT id, question, options, closed FROM polls WHERE id = %s', [pid])
        if not rows:
            raise ApiError('この投票は見つかりません', 404)
        opts = rows[0]['options'] if isinstance(rows[0]['options'], list) else json.loads(rows[0]['options'])
        groups = [{'text': t, 'voters': []} for t in opts]
        for v in db_query('SELECT user_id, option_idx, voted_at FROM poll_votes WHERE poll_id = %s ORDER BY voted_at ASC', [pid]):
            if 0 <= v['option_idx'] < len(groups):
                at = v.get('voted_at')
                groups[v['option_idx']]['voters'].append({'id': v['user_id'], 'at': at.isoformat() if hasattr(at, 'isoformat') else None})
        return jsonify({'id': pid, 'question': rows[0]['question'], 'closed': bool(rows[0]['closed']),
                        'total': sum(len(g['voters']) for g in groups), 'options': groups})

    @app.post('/api/admin/poll/create')
    @guard
    def poll_create():
        uid = require_admin()
        b = request.get_json(silent=True) or {}
        q, opts = clean_question(b.get('question')), clean_options(b.get('options'))
        rows = db_query('INSERT INTO polls (question, options, created_by) VALUES (%s, %s, %s) RETURNING id', [q, json.dumps(opts, ensure_ascii=False), uid])
        return jsonify({'ok': True, 'id': rows[0]['id']})

    @app.post('/api/admin/poll/update')
    @guard
    def poll_update():
        require_admin()
        b = request.get_json(silent=True) or {}
        pid = int(b.get('id') or 0)
        q, opts = clean_question(b.get('question')), clean_options(b.get('options'))
        if not db_query('SELECT id FROM polls WHERE id = %s', [pid]):
            raise ApiError('この投票は見つかりません', 404)
        db_query('UPDATE polls SET question = %s, options = %s WHERE id = %s RETURNING id', [q, json.dumps(opts, ensure_ascii=False), pid])
        db_query('DELETE FROM poll_votes WHERE poll_id = %s AND option_idx >= %s RETURNING poll_id', [pid, len(opts)])   # 減らした選択肢への票は消す
        return jsonify({'ok': True})

    @app.post('/api/admin/poll/close')
    @guard
    def poll_close():
        require_admin()
        b = request.get_json(silent=True) or {}
        db_query('UPDATE polls SET closed = %s WHERE id = %s RETURNING id', [bool(b.get('closed', True)), int(b.get('id') or 0)])
        return jsonify({'ok': True})

    @app.post('/api/admin/poll/reset')
    @guard
    def poll_reset():
        require_admin()
        b = request.get_json(silent=True) or {}
        db_query('DELETE FROM poll_votes WHERE poll_id = %s RETURNING poll_id', [int(b.get('id') or 0)])
        return jsonify({'ok': True})

    @app.post('/api/admin/poll/delete')
    @guard
    def poll_delete():
        require_admin()
        b = request.get_json(silent=True) or {}
        db_query('DELETE FROM polls WHERE id = %s RETURNING id', [int(b.get('id') or 0)])
        return jsonify({'ok': True})

    # ---------------------------------------------------------------- ギフト
    @app.post('/api/admin/gift')
    @guard
    def gift_send():
        uid = require_admin()
        b = request.get_json(silent=True) or {}
        count, batch = create_gift(db_query, uid, b.get('to'), b.get('message'),
                                   b.get('coins'), b.get('candies'), b.get('bpXp'), b.get('eventXp'))
        return jsonify({'ok': True, 'count': count, 'batch': batch})

    @app.get('/api/admin/gifts')
    @guard
    def gift_history():
        require_admin()
        rows = db_query("SELECT batch_id, MIN(message) AS message, MIN(coins) AS coins, MIN(candies) AS candies, MIN(bp_xp) AS bp_xp, "
                        "MIN(event_xp) AS event_xp, MIN(to_user) AS to_user, COUNT(*) AS total, COUNT(claimed_at) AS claimed, MIN(created_at) AS created_at "
                        "FROM gifts GROUP BY batch_id ORDER BY MIN(created_at) DESC LIMIT 30")
        out = []
        for r in rows:
            ca = r.get('created_at')
            out.append({'batch': r['batch_id'], 'message': r['message'], **gift_items(r), 'to': r['to_user'] if int(r['total']) == 1 else 'all',
                        'total': int(r['total']), 'claimed': int(r['claimed']), 'createdAt': ca.isoformat() if hasattr(ca, 'isoformat') else None})
        return jsonify({'gifts': out})

    @app.post('/api/admin/gift/cancel')
    @guard
    def gift_cancel():
        require_admin()
        b = request.get_json(silent=True) or {}
        rows = db_query('DELETE FROM gifts WHERE batch_id = %s AND claimed_at IS NULL RETURNING id', [str(b.get('batch') or '')])
        return jsonify({'ok': True, 'cancelled': len(rows)})

    @app.get('/api/gifts/pending')
    @guard
    def gifts_pending():
        user = require_auth()
        rows = db_query('SELECT id, from_user, message, coins, candies, bp_xp, event_xp, created_at FROM gifts '
                        'WHERE to_user = %s AND claimed_at IS NULL ORDER BY id ASC LIMIT 20', [user])
        out = []
        for r in rows:
            ca = r.get('created_at')
            out.append({'id': r['id'], 'from': r['from_user'], 'message': r['message'], **gift_items(r),
                        'createdAt': ca.isoformat() if hasattr(ca, 'isoformat') else None})
        return jsonify({'gifts': out})

    @app.post('/api/gifts/claim')
    @guard
    def gifts_claim():
        user = require_auth()
        b = request.get_json(silent=True) or {}
        try:
            gid = int(b.get('id'))
        except (TypeError, ValueError):
            raise ApiError('ギフトが不正です', 400)
        with db_transaction() as cur:
            cur.execute('SELECT id, coins, candies, bp_xp, event_xp FROM gifts WHERE id = %s AND to_user = %s AND claimed_at IS NULL FOR UPDATE', [gid, user])
            gift = cur.fetchone()
            if not gift:
                raise ApiError('このギフトは受け取り済みか、存在しません', 404)
            row = H.lock_user(cur, user)
            if not row:
                raise ApiError('ユーザーが見つかりません', 404)
            s = H.load_state_from_row(row)
            it = apply_gift(s, gift)
            H.save_user(cur, user, s)
            cur.execute('UPDATE gifts SET claimed_at = NOW() WHERE id = %s', [gid])
        return jsonify({'ok': True, 'items': it, 'coins': s['coins'], 'battlepassXp': s['bpxp'],
                        'candies': s['st']['candies'], 'totalCandies': s['st']['totalCandies']})

    # ---------------------------------------------------------------- メッセージの編集・取り消し
    def edit_msg(table, id_col_user, limit):
        b = request.get_json(silent=True) or {}
        user = require_auth()
        try:
            mid = int(b.get('id'))
        except (TypeError, ValueError):
            raise ApiError('メッセージが不正です', 400)
        text = (b.get('message') or '').strip()
        if not text:
            raise ApiError('メッセージを入力してください', 400)
        if len(text) > limit:
            raise ApiError(f'メッセージは{limit}文字以内にしてください', 400)
        rows = db_query(f'SELECT {id_col_user} AS owner, COALESCE(deleted, FALSE) AS deleted FROM {table} WHERE id = %s', [mid])
        if not rows or rows[0]['owner'] != user:
            raise ApiError('自分のメッセージだけ編集できます', 403)
        if rows[0]['deleted']:
            raise ApiError('取り消したメッセージは編集できません', 400)
        db_query(f'UPDATE {table} SET message = %s, edited_at = NOW() WHERE id = %s RETURNING id', [text, mid])
        if on_chat_change: on_chat_change()
        return jsonify({'ok': True})

    def retract_msg(table, id_col_user):
        b = request.get_json(silent=True) or {}
        user = require_auth()
        try:
            mid = int(b.get('id'))
        except (TypeError, ValueError):
            raise ApiError('メッセージが不正です', 400)
        rows = db_query(f'SELECT {id_col_user} AS owner FROM {table} WHERE id = %s', [mid])
        if not rows or rows[0]['owner'] != user:
            raise ApiError('自分のメッセージだけ取り消せます', 403)
        db_query(f"UPDATE {table} SET deleted = TRUE, message = '' WHERE id = %s RETURNING id", [mid])
        if on_chat_change: on_chat_change()
        return jsonify({'ok': True})

    @app.post('/api/chat/edit')
    @guard
    def chat_edit(): return edit_msg('chat_messages', 'user_id', CHAT_MAX)

    @app.post('/api/chat/retract')
    @guard
    def chat_retract(): return retract_msg('chat_messages', 'user_id')

    @app.post('/api/dm/edit')
    @guard
    def dm_edit(): return edit_msg('dm_messages', 'from_id', DM_MAX)

    @app.post('/api/dm/retract')
    @guard
    def dm_retract(): return retract_msg('dm_messages', 'from_id')

    # ---------------------------------------------------------------- 管理者: ユーザーを選ぶ
    @app.get('/api/admin/users')
    @guard
    def admin_users():
        require_admin()
        q = re.sub(r'([%_\\])', r'\\\1', (request.args.get('q') or '').strip()[:30])
        rows = db_query("SELECT id, best_score, coins, COALESCE(banned, FALSE) AS banned FROM users WHERE id ILIKE %s "
                        "ORDER BY last_login DESC NULLS LAST, id ASC LIMIT 40", [f'%{q}%'])
        return jsonify({'users': [{'id': r['id'], 'bestScore': int(r.get('best_score') or 0), 'coins': int(r.get('coins') or 0), 'banned': bool(r['banned'])} for r in rows]})
