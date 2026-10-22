"""
CandyBlast サーバー (Python / Flask 版)
元は Node.js / Express で書かれていた server.js を、機能・挙動をできるだけ
そのまま保ったうえで Python に移植したものです。
フロントエンド(index.html / styles.css / script.js)は変更していません。
"""
import os
import re
import json
import base64
import random
import string
import secrets
import threading
import time
import gzip
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import jwt
import bcrypt
import requests
import psycopg2
from flask import Flask, request, jsonify, g, has_request_context
from flask_cors import CORS
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor
from apscheduler.schedulers.background import BackgroundScheduler

# ===================== 基本設定 =====================
app = Flask(__name__)

# GitHub Pages など、フロントエンドを置くオリジンだけに絞りたい場合は
# 環境変数 ALLOWED_ORIGINS にカンマ区切りで指定する(例: https://ユーザー名.github.io)。
# 未設定なら従来どおり全オリジン許可。
_allowed = [o.strip() for o in os.environ.get('ALLOWED_ORIGINS', '').split(',') if o.strip()]
# max_age: ブラウザに「事前確認(プリフライト)の結果」を覚えさせる。無いと、GitHub Pages → Render の呼び出しのたびに
#          本番のリクエストの前に OPTIONS が1往復余計に走っていた(Chromeは最大2時間まで覚える)
CORS(app, origins=_allowed or '*', max_age=7200, expose_headers=['Server-Timing'])

DATABASE_URL = os.environ.get('DATABASE_URL')
JWT_SECRET = os.environ.get('JWT_SECRET', 'your-secret-key')

# ★管理者アカウントを 'admin' から 'spirit' へ移行。
#   ここを1箇所変えるだけで管理者判定・全スキン無料付与などが全て切り替わる。
ADMIN_USER_ID = 'spirit'

def _clean_webhook(raw):
    """環境変数に貼ったURLの、前後の空白・改行・引用符を取り除く(コピー時に混ざりやすく、これだけで送信エラーになる)。"""
    u = (raw or '').strip().strip('\'"').strip()
    u = u.split()[0] if u else ''
    return u.replace('://discordapp.com/', '://discord.com/')


DISCORD_WEBHOOK_AUTH = _clean_webhook(os.environ.get('DISCORD_WEBHOOK_AUTH'))
DISCORD_WEBHOOK_LOGIN = _clean_webhook(os.environ.get('DISCORD_WEBHOOK_LOGIN'))
DISCORD_WEBHOOK_FEATURE_REQUEST = _clean_webhook(os.environ.get('DISCORD_WEBHOOK_FEATURE_REQUEST'))
DISCORD_WEBHOOK_BUG_REPORT = _clean_webhook(os.environ.get('DISCORD_WEBHOOK_BUG_REPORT'))

if not DATABASE_URL:
    print('❌ 環境変数 DATABASE_URL が設定されていません。RenderのEnvironmentタブでSupabaseの接続文字列を設定してください。')
if JWT_SECRET == 'your-secret-key':
    print('⚠️ JWT_SECRET が未設定です。必ず推測されにくい値を環境変数に設定してください。')

# ===================== DB接続プール(Supabase Postgres) =====================
# DATABASE_URL には Supabase の「Session pooler」または「Transaction pooler」の接続文字列を使う。
# (Renderの無料プランはIPv4のみのため、IPv6専用の Direct connection は使えない)
# Supabase は SSL 必須。プーラーが長時間アイドルの接続を切ることがあるため、
# TCP keepalive を有効にし、切れた接続は捨てて1回だけ自動リトライする。
DB_POOL_MAX = int(os.environ.get('DB_POOL_MAX', '10'))
DB_POOL_MIN = int(os.environ.get('DB_POOL_MIN', '4'))   # 先に繋いでおく本数(接続のたびにSSLを張る待ち時間をなくす)
_pool = None
_pool_sem = threading.BoundedSemaphore(DB_POOL_MAX)     # 満杯のときはエラーにせず、空くまで待つ
if DATABASE_URL:
    _pool = pg_pool.ThreadedConnectionPool(
        min(DB_POOL_MIN, DB_POOL_MAX), DB_POOL_MAX, dsn=DATABASE_URL, sslmode='require', connect_timeout=10,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5
    )

_CONN_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


_last_used = {}            # 接続ごとの最後に使った時刻(長いあいだ使っていない接続は、使う前に生きているか確かめる)
STALE_AFTER = 120          # 秒。Supabase のプーラーは、使われていない接続を黙って切ることがある
if _pool is not None:
    try:
        for _c in _pool._pool:
            _last_used[id(_c)] = time.monotonic()
    except Exception:
        pass


def _release(conn, broken):
    try:
        if broken or conn.closed:
            _last_used.pop(id(conn), None)
        else:
            _last_used[id(conn)] = time.monotonic()
        _pool.putconn(conn, close=bool(broken or conn.closed))
    finally:
        _pool_sem.release()


def _getconn():
    _pool_sem.acquire()
    try:
        for _ in range(DB_POOL_MAX + 1):
            conn = _pool.getconn()
            last = _last_used.get(id(conn))
            if last is not None and time.monotonic() - last > STALE_AFTER:
                try:                                   # 久しぶりの接続は、軽く確かめてから使う
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute('SELECT 1')
                except Exception:
                    _last_used.pop(id(conn), None)
                    _pool.putconn(conn, close=True)   # 死んでいたら捨てて、新しい接続に替える
                    continue
            return conn
        raise psycopg2.OperationalError('DB接続を取得できませんでした')
    except Exception:
        _pool_sem.release()
        raise


def _run_query(sql, params, fetch):
    _t0 = time.perf_counter()
    conn = _getconn()
    broken = False
    try:
        # autocommit: 1文だけの問い合わせは、BEGIN/COMMIT の往復を省く(COMMIT のぶん、DBとの往復が1回減る)
        if not conn.autocommit:
            conn.autocommit = True
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or [])
            rows = cur.fetchall() if (fetch and cur.description is not None) else []
        return rows
    except _CONN_ERRORS:
        broken = True
        raise
    finally:
        _release(conn, broken)
        if has_request_context():       # このリクエストで DB に使った時間を足す(下の Server-Timing 用)
            g.db_ms = getattr(g, 'db_ms', 0.0) + (time.perf_counter() - _t0) * 1000
            g.db_n = getattr(g, 'db_n', 0) + 1


def db_query(sql, params=None, fetch=True):
    """1クエリを実行するショートカット。SELECT系はdictのリストを返す。
    接続が切れていたら捨てて再試行する(読むだけの問い合わせは2回まで、書き込みは二重実行を避けて1回まで)。"""
    retries = 2 if sql.lstrip()[:6].upper() in ('SELECT', 'WITH S') else 1
    for attempt in range(retries + 1):
        try:
            return _run_query(sql, params, fetch)
        except _CONN_ERRORS:
            if attempt >= retries:
                raise


def db_execute(sql, params=None):
    db_query(sql, params, fetch=False)


@contextmanager
def db_transaction():
    """複数クエリを1トランザクションで実行する(SELECT ... FOR UPDATE で二重消費を防ぐ用)。
    with ブロックを正常に抜けるとcommit、例外ならrollbackする。"""
    conn = _getconn()
    broken = False
    try:
        if conn.autocommit:
            conn.autocommit = False      # 複数クエリをひとまとめにするときだけ、通常のトランザクションに戻す
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except _CONN_ERRORS:
        broken = True
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            broken = True
        raise
    finally:
        _release(conn, broken)


# ===================== 短時間キャッシュ(同じ内容を全員が何度も聞くAPI用) =====================
# 例: ランキング・チャット・イベント一覧は、全員が数秒おきに同じ内容を取りに来る。
# 数秒だけ結果を覚えておけば、DBへの問い合わせが「人数ぶん」から「数秒に1回」に減る。
_cache = {}
_cache_lock = threading.Lock()


def ttl_cache(key, ttl, fn):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    with _cache_lock:
        if len(_cache) > 300:          # 溜まりすぎ防止
            _cache.clear()
        _cache[key] = (time.monotonic(), val)
    return val


def cache_drop(*prefixes):
    with _cache_lock:
        for k in list(_cache):
            if any(str(k).startswith(p) for p in prefixes):
                _cache.pop(k, None)


# ===================== Discord通知 =====================
def to_copy_block(text, limit=1500):
    """ご要望・不具合の本文を、コピーしやすい形で送る。
    ・💻 PC用: コードブロック(```)。ブロック右上にマウスを乗せると「コピー」ボタンが出る(```でのコピーはPCのみ)
    ・📱 スマホ用: 1行ごとに「`」1個で囲む(インラインコード)。タップするとコピーできる
    本文に ``` や ` が含まれると囲みが壊れるので、無害な文字に置き換える。"""
    body = (text or '').strip()[:limit]
    pc = body.replace('```', "'''")
    lines = [ln.strip().replace('`', '´') for ln in body.splitlines() if ln.strip()]
    mobile = '\n'.join(f'`{ln}`' for ln in lines[:40])
    return ('💻 **PC用**(右上のコピーボタンをクリック)\n```\n' + pc + '\n```\n'
            '📱 **スマホ用**(タップでコピー)\n' + mobile)


# --- 送信の仕組み ---
#  ・ログイン/登録/再設定の通知は「裏で送る」(Discordの応答を待たない → ログインなどが速くなる)
#  ・ご要望/不具合の報告は、成功したか失敗したかを利用者に返すので、その場で送る(wait=True)
#  ・429(送りすぎ)は待ち時間(retry_after)のあとに、5xx・通信エラーは少し待って、最大3回までやり直す
#  ・失敗した理由は Render のログに「Discord通知失敗(宛先): …」と出る。管理者コマンド /discordtest で全宛先を一括確認できる
_discord_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='discord')
_discord_last_error = {}
_DISCORD_HEADERS = {'User-Agent': 'DiscordBot (https://github.com/candyblast, 1.0)'}
_DISCORD_HOOKS = {'AUTH': 'DISCORD_WEBHOOK_AUTH', 'LOGIN': 'DISCORD_WEBHOOK_LOGIN',
                  'FEATURE_REQUEST': 'DISCORD_WEBHOOK_FEATURE_REQUEST', 'BUG_REPORT': 'DISCORD_WEBHOOK_BUG_REPORT'}


def _discord_label(url):
    for name, env in _DISCORD_HOOKS.items():
        if globals().get(env) == url:
            return name
    return 'unknown'


def _discord_hint(status):
    if status in (401, 404):
        return '(Webhookが削除されたか、URLが違います。Discordで作り直して環境変数を更新してください)'
    if status == 400:
        return '(送った内容の形式エラーです)'
    return ''


def _discord_send_now(webhook_url, embed, image_bytes, image_filename, image_mime_type):
    label = _discord_label(webhook_url)
    last = ''
    for attempt in range(3):
        try:
            if image_bytes:
                # payload_json側に attachments 配列で「どのファイルがどのattachment idか」を明示しないと、画像が届かないことがある
                embed = dict(embed, image={'url': f'attachment://{image_filename}'})
                payload = {'embeds': [embed], 'attachments': [{'id': 0, 'filename': image_filename}], 'allowed_mentions': {'parse': []}}
                resp = requests.post(webhook_url, data={'payload_json': json.dumps(payload)},
                                     files={'files[0]': (image_filename, image_bytes, image_mime_type)},
                                     headers=_DISCORD_HEADERS, timeout=(5, 20))
            else:
                resp = requests.post(webhook_url, json={'embeds': [embed], 'allowed_mentions': {'parse': []}},
                                     headers=_DISCORD_HEADERS, timeout=(5, 15))
            if resp.status_code < 300:
                _discord_last_error.pop(label, None)
                return True
            last = f'HTTP {resp.status_code} {resp.text[:300]}{_discord_hint(resp.status_code)}'
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get('retry_after', 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(max(wait, 0.5), 5))
                continue
            if resp.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            break                                   # 400/401/403/404 などは、やり直しても直らない
        except requests.RequestException as err:
            last = f'通信エラー: {err}'
            time.sleep(1 + attempt)
    _discord_last_error[label] = last
    print(f'Discord通知失敗({label}): {last}')
    return False


def send_discord_notification(webhook_url, title, description, color=0x5865F2,
                               fields=None, image_bytes=None, image_filename='image.png',
                               image_mime_type='image/png', wait=False):
    """埋め込みメッセージ(必要なら画像添付)をDiscordのWebhookへ送る。
    wait=True: 送り終わるまで待って、成功したかを返す / wait=False: 裏で送る(戻り値は「受け付けたか」)"""
    if not webhook_url:
        print('Discord通知スキップ: Webhook URL が未設定です(RenderのEnvironmentを確認してください)')
        return False
    # Discord の上限に収める(超えると 400 で丸ごと拒否される): タイトル256 / 本文4096 / 項目名256 / 項目値1024 / 項目25個まで
    safe_fields = []
    for f in (fields or [])[:25]:
        safe_fields.append({'name': (str(f.get('name') or '-').strip() or '-')[:256],
                            'value': (str(f.get('value') or '-').strip() or '-')[:1024],
                            'inline': bool(f.get('inline'))})
    embed = {
        'title': str(title)[:256], 'description': str(description or '-')[:4096], 'color': color,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'fields': safe_fields, 'footer': {'text': 'CandyBlast System'}
    }
    args = (webhook_url, embed, image_bytes, image_filename, image_mime_type)
    if wait:
        return _discord_send_now(*args)
    try:
        _discord_pool.submit(_discord_send_now, *args)
        return True
    except Exception as err:
        print(f'Discord通知失敗(キュー): {err}')
        return False


def discord_status_text():
    lines = []
    for name, env in _DISCORD_HOOKS.items():
        url = globals().get(env)
        if not url:
            lines.append(f'・{name}: ⚪ 未設定({env})')
        elif not url.startswith('https://discord.com/api/webhooks/'):
            lines.append(f'・{name}: ❌ URLの形式が違います(https://discord.com/api/webhooks/… で始まる必要があります)')
        else:
            lines.append(f'・{name}: 設定あり')
    return '\n'.join(lines)


print('Discord Webhook 設定:\n' + discord_status_text())


# ===================== 認証ヘルパー =====================
class ApiError(Exception):
    def __init__(self, message, status=500):
        super().__init__(message)
        self.message = message
        self.status = status


def make_token(user_id):
    payload = {'id': user_id, 'exp': datetime.utcnow() + timedelta(days=7)}
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')


def get_token_from_request():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[len('Bearer '):]
    return None


def require_auth():
    token = get_token_from_request()
    if not token:
        raise ApiError('認証が必要です', 401)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload['id']
    except Exception:
        raise ApiError('認証エラー', 401)


def get_user_id_optional():
    token = get_token_from_request()
    if not token:
        return 'ゲスト'
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
    except Exception:
        return 'ゲスト'


def are_friends(a, b):
    rows = db_query('SELECT 1 FROM friends WHERE user_id = %s AND friend_id = %s', [a, b])
    return len(rows) > 0


def generate_recovery_code():
    parts = []
    for _ in range(4):
        chars = string.ascii_uppercase + string.digits
        parts.append(''.join(secrets.choice(chars) for _ in range(4)))
    return '-'.join(parts)


def hash_password(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(10)).decode('utf-8')


def check_password(password, hashed):
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


# ===================== テーブル作成(起動時に順番に実行) =====================
# Node版と同じく、CREATE TABLE IF NOT EXISTS は既存テーブルには効かないため、
# 後から追加した列は必ず ALTER TABLE ... ADD COLUMN IF NOT EXISTS で追加する。
def init_db():
    db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            recovery_code TEXT,
            recovery_code_used BOOLEAN DEFAULT FALSE,
            best_score INTEGER DEFAULT 0,
            coins INTEGER DEFAULT 0,
            skins TEXT DEFAULT '["default"]',
            equipped_skin TEXT DEFAULT 'default',
            quests TEXT DEFAULT '[]',
            last_login TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    db_execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS best_scores JSONB DEFAULT '{}';
        ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_settings JSONB DEFAULT '{"disabledBlocks":[],"safetyMode":false}';
        ALTER TABLE users ADD COLUMN IF NOT EXISTS quest_progress JSONB DEFAULT '{}';
        ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT FALSE;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS play_time INTEGER DEFAULT 0;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP;
        ALTER TABLE users ALTER COLUMN coins TYPE BIGINT;
        ALTER TABLE users ALTER COLUMN best_score TYPE BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS gacha_state JSONB DEFAULT '{"totalPulls":0,"pityCounter":0}';
        ALTER TABLE users ADD COLUMN IF NOT EXISTS battlepass_xp BIGINT DEFAULT 0;
        ALTER TABLE users ALTER COLUMN battlepass_xp TYPE BIGINT;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS battlepass_claimed JSONB DEFAULT '[]';
        ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_forced_scores JSONB DEFAULT '{}';
    """)
    db_execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS titles JSONB DEFAULT '[]'::jsonb;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS equipped_title TEXT;
    """)
    print('✅ users テーブル準備完了')

    db_execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, message TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_chat_timestamp ON chat_messages(timestamp);
    """)
    print('✅ チャットテーブル作成完了')

    db_execute("""
        CREATE TABLE IF NOT EXISTS friend_requests (
            id SERIAL PRIMARY KEY, from_id TEXT NOT NULL, to_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(from_id, to_id)
        );
        CREATE TABLE IF NOT EXISTS friends (
            id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, friend_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(), UNIQUE(user_id, friend_id)
        );
        CREATE TABLE IF NOT EXISTS dm_messages (
            id SERIAL PRIMARY KEY, from_id TEXT NOT NULL, to_id TEXT NOT NULL,
            message TEXT NOT NULL, is_read BOOLEAN DEFAULT FALSE, timestamp TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_dm_pair ON dm_messages(from_id, to_id);
    """)
    print('✅ フレンド／DMテーブル作成完了')

    db_execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id SERIAL PRIMARY KEY, message TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    print('✅ お知らせテーブル作成完了')

    db_execute("""
        CREATE TABLE IF NOT EXISTS duels (
            id SERIAL PRIMARY KEY, challenger_id TEXT NOT NULL, opponent_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending', duration INTEGER DEFAULT 60,
            challenger_score INTEGER, opponent_score INTEGER, created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    print('✅ 対決(デュエル)テーブル作成完了')

    db_execute("""
        CREATE TABLE IF NOT EXISTS scheduled_events (
            id SERIAL PRIMARY KEY, hour_jst INTEGER NOT NULL, ranking_mode TEXT NOT NULL,
            rank_position INTEGER NOT NULL, reward_coins INTEGER NOT NULL,
            last_triggered_date TEXT, created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    db_execute("""
        ALTER TABLE scheduled_events ADD COLUMN IF NOT EXISTS recurrence TEXT DEFAULT 'daily';
        ALTER TABLE scheduled_events ADD COLUMN IF NOT EXISTS minute_jst INTEGER DEFAULT 0;
        ALTER TABLE scheduled_events ADD COLUMN IF NOT EXISTS event_year INTEGER;
        ALTER TABLE scheduled_events ADD COLUMN IF NOT EXISTS event_month INTEGER;
        ALTER TABLE scheduled_events ADD COLUMN IF NOT EXISTS event_day INTEGER;
    """)
    print('✅ スケジュールイベントテーブル作成完了')

    db_execute("""
        CREATE TABLE IF NOT EXISTS weekly_challenge (
            id SERIAL PRIMARY KEY, week_key TEXT UNIQUE NOT NULL, mode TEXT NOT NULL,
            size INTEGER NOT NULL, created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS weekly_challenge_scores (
            id SERIAL PRIMARY KEY, week_key TEXT NOT NULL, user_id TEXT NOT NULL,
            score INTEGER NOT NULL, created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(week_key, user_id)
        );
    """)
    print('✅ 週替わりチャレンジテーブル作成完了')

    # 🕹 ミニゲームのランキング
    db_execute('''
        CREATE TABLE IF NOT EXISTS mini_scores (
            game TEXT NOT NULL, user_id TEXT NOT NULL, best BIGINT NOT NULL,
            plays INTEGER DEFAULT 0, updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (game, user_id)
        );
    ''')

    # 🎃 イベント進行状況 / フレンド対決のゲーム種別
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS event_state JSONB DEFAULT '{}'::jsonb;")
    db_execute("ALTER TABLE duels ADD COLUMN IF NOT EXISTS game TEXT DEFAULT 'candy';")

    # 🗳 投票 / 🎁 ギフト / ✏️ チャット・DMの編集・取り消し用の列
    social.init_tables(db_execute)
    engage.init_tables(db_execute)          # 📆 毎日ログインボーナス

    # ⚡ 読み込みを速くするための索引(無くても動くが、データが増えるほど遅くなる)
    db_execute('''
        CREATE INDEX IF NOT EXISTS idx_friend_req_to ON friend_requests (to_id, status);
        CREATE INDEX IF NOT EXISTS idx_friend_req_from ON friend_requests (from_id, status);
        CREATE INDEX IF NOT EXISTS idx_dm_to_unread ON dm_messages (to_id, is_read);
        CREATE INDEX IF NOT EXISTS idx_dm_pair_ts ON dm_messages (from_id, to_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_users_coins ON users (coins DESC);
        CREATE INDEX IF NOT EXISTS idx_users_playtime ON users (play_time DESC);
        CREATE INDEX IF NOT EXISTS idx_poll_votes_user ON poll_votes (user_id);
    ''')

    # Supabase では public スキーマのテーブルが Data API(anonキー)から見えうるため、
    # 全テーブルでRLSを有効化(ポリシーなし = anon/authenticated は一切アクセス不可)。
    # このサーバーは postgres ロールで直接接続するのでRLSの影響を受けない。
    for t in ('users', 'chat_messages', 'friend_requests', 'friends', 'dm_messages',
              'announcements', 'duels', 'scheduled_events', 'weekly_challenge',
              'weekly_challenge_scores', 'mini_scores', 'daily_bonus'):
        db_execute(f'ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;')
    print('✅ RLS有効化完了')


# ===================== 日本時間(JST)ヘルパー =====================
JST = ZoneInfo('Asia/Tokyo')


def jst_now():
    return datetime.now(JST)


def jst_date_key(d):
    return f'{d.year}-{d.month:02d}-{d.day:02d}'


# ===================== ガチャ・バトルパスの定数 =====================

GACHA_SKIN_IDS = [
    'gacha_cosmicdragon', 'gacha_celestialphoenix', 'gacha_voidempress',
    'gacha_abyssleviathan', 'gacha_thunderlord', 'gacha_celestialblossom',
    'gacha_eternalclockwork', 'gacha_prismaticqueen',
]

import halloween  # 🎃 期間限定イベント(別ファイル)
import social  # 💬🗳🎁 投票・ギフト・メッセージ編集(別ファイル)
import titles  # 🏷 称号(別ファイル)
import engage  # 🎁 毎日ログインボーナス・ウェルカムギフト(別ファイル)
GACHA_SKIN_ID_SET = set(GACHA_SKIN_IDS) | halloween.ALL_SKIN_IDS   # 自称で所持できない(サーバー付与のみ)スキン
GACHA_COST = 2000
GACHA_RATE_PER_SKIN = 0.004  # 1スキンあたり0.4%(8種で合計3.2%) ← 一般的なソシャゲの最高レア帯
GACHA_PITY_THRESHOLD = 1000
GACHA_DUPLICATE_COINS = 500
GACHA_MISS_COINS = 30

BATTLEPASS_XP_PER_LEVEL = 1000
BATTLEPASS_CYCLE_LENGTH = 10
WEEKLY_CHALLENGE_REWARD = 1500


def battle_pass_reward_for_level(level):
    cycle_pos = ((level - 1) % BATTLEPASS_CYCLE_LENGTH) + 1
    rewards = {
        1: {'type': 'coins', 'amount': 100}, 2: {'type': 'coins', 'amount': 150},
        3: {'type': 'coins', 'amount': 200}, 4: {'type': 'coins', 'amount': 250},
        5: {'type': 'gacha_ticket', 'amount': 1}, 6: {'type': 'coins', 'amount': 300},
        7: {'type': 'coins', 'amount': 350}, 8: {'type': 'coins', 'amount': 400},
        9: {'type': 'coins', 'amount': 500}, 10: {'type': 'gacha_ticket', 'amount': 1},
    }
    r = dict(rewards[cycle_pos])
    r['level'] = level
    return r


# ===================== 🔎 速度の計測(Server-Timing) =====================
# 各レスポンスに「このリクエストでDBに何ms・何回使ったか / サーバー全体で何msか」を付ける。
# ブラウザの開発者ツール → Network → 遅いAPIを選ぶ → Timing で見られる。
#   db が大きい → DB(Supabase)側が遅い / app-db が大きい → サーバー(Render)側が遅い /
#   どちらも小さいのに遅い → ブラウザ↔サーバー間の通信距離(リージョン違い)や、サーバーの起動待ち
@app.before_request
def _timing_start():
    g.t0 = time.perf_counter()


@app.after_request
def _timing_end(resp):
    try:
        total = (time.perf_counter() - g.t0) * 1000
        db = getattr(g, 'db_ms', 0.0)
        # ※ HTTPヘッダーは英数字だけ(日本語を入れると、本番のgunicornで全APIがエラーになる)
        resp.headers['Server-Timing'] = f'db;dur={db:.0f};desc="queries {getattr(g, "db_n", 0)}", app;dur={max(0, total - db):.0f}, total;dur={total:.0f}'
        if total > 800:
            print(f'🐢 遅いリクエスト {request.method} {request.path}: 合計{total:.0f}ms(DB {db:.0f}ms / {getattr(g, "db_n", 0)}回)')
    except Exception:
        pass
    return resp


# ===================== 📦 JSONの圧縮(gzip) =====================
# 投票・バトルパス・チャットなど、1KBを超える応答は gzip で小さくして送る(モバイル回線で効く)
@app.after_request
def _gzip_json(resp):
    try:
        if (resp.status_code == 200 and not resp.direct_passthrough and resp.mimetype == 'application/json'
                and 'gzip' in request.headers.get('Accept-Encoding', '') and 'Content-Encoding' not in resp.headers):
            data = resp.get_data()
            if len(data) > 1024:
                resp.set_data(gzip.compress(data, compresslevel=5))
                resp.headers['Content-Encoding'] = 'gzip'
                resp.headers['Vary'] = 'Accept-Encoding'
    except Exception:
        pass
    return resp


# ===================== 共通エラーハンドラ =====================
@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify({'error': err.message}), err.status


# ===================== ユーザー登録 =====================
@app.post('/api/register')
def register():
    body = request.get_json(silent=True) or {}
    user_id = body.get('id')
    password = body.get('password')
    if not user_id or not password or len(password) < 6:
        return jsonify({'error': 'IDとパスワード(6文字以上)が必要です'}), 400
    if not re.match(r'^[a-zA-Z0-9_]{3,20}$', user_id):
        return jsonify({'error': 'IDは半角英数字とアンダースコアで3〜20文字です'}), 400
    try:
        if db_query('SELECT * FROM users WHERE id = %s', [user_id]):
            return jsonify({'error': 'このIDは既に使われています'}), 400
        pw_hash = hash_password(password)
        recovery_code = generate_recovery_code()
        db_execute(
            'INSERT INTO users (id, password_hash, recovery_code, recovery_code_used) VALUES (%s, %s, %s, %s)',
            [user_id, pw_hash, recovery_code, False]
        )
        token = make_token(user_id)
        engage.send_welcome_gift(db_query, user_id)      # 🎁 ウェルカムギフト(ログイン後に開封できる)
        send_discord_notification(
            DISCORD_WEBHOOK_AUTH, '📝 新規ユーザー登録', f'ユーザー **{user_id}** が新規登録しました！', 0x00FF00,
            [{'name': '🔑 復元コード', 'value': f'`{recovery_code}`', 'inline': False},
             {'name': '📅 登録日時', 'value': datetime.now().strftime('%Y/%m/%d %H:%M:%S'), 'inline': True}]
        )
        return jsonify({'token': token, 'id': user_id, 'recoveryCode': recovery_code})
    except Exception as err:
        print(err)
        return jsonify({'error': 'サーバーエラー'}), 500


# ===================== ログイン =====================
@app.post('/api/login')
def login():
    body = request.get_json(silent=True) or {}
    user_id = body.get('id')
    password = body.get('password')
    try:
        rows = db_query('SELECT * FROM users WHERE id = %s', [user_id])
        if not rows:
            return jsonify({'error': 'IDまたはパスワードが間違っています'}), 401
        user = rows[0]
        if user.get('banned'):
            return jsonify({'error': 'このアカウントはBANされています'}), 403
        if not password or not check_password(password, user['password_hash']):
            return jsonify({'error': 'IDまたはパスワードが間違っています'}), 401
        db_execute('UPDATE users SET last_login = NOW() WHERE id = %s', [user_id])
        token = make_token(user_id)
        send_discord_notification(
            DISCORD_WEBHOOK_LOGIN, '🔐 ログイン検出', f'ユーザー **{user_id}** がログインしました。', 0x5865F2,
            [{'name': '🕐 ログイン日時', 'value': datetime.now().strftime('%Y/%m/%d %H:%M:%S'), 'inline': True},
             {'name': '🏆 現在のベストスコア', 'value': f"{user.get('best_score') or 0}点", 'inline': True}]
        )
        return jsonify({
            'token': token, 'id': user_id,
            'bestScore': user.get('best_score'), 'bestScores': user.get('best_scores') or {},
            'coins': int(user.get('coins') or 0), 'playTime': user.get('play_time') or 0,
            'lastLogin': user.get('last_login').isoformat() if user.get('last_login') else None
        })
    except Exception as err:
        print(err)
        return jsonify({'error': 'サーバーエラー'}), 500


# ===================== パスワード復元 =====================
@app.post('/api/recover')
def recover():
    body = request.get_json(silent=True) or {}
    user_id = body.get('id')
    recovery_code = body.get('recoveryCode')
    new_password = body.get('newPassword')
    if not new_password or len(new_password) < 6:
        return jsonify({'error': '新しいパスワードは6文字以上が必要です'}), 400
    try:
        rows = db_query(
            'SELECT * FROM users WHERE id = %s AND recovery_code = %s AND recovery_code_used = false',
            [user_id, recovery_code]
        )
        if not rows:
            return jsonify({'error': '無効な復元コードです（既に使われたか期限切れです）'}), 400
        pw_hash = hash_password(new_password)
        new_recovery_code = generate_recovery_code()
        db_execute(
            'UPDATE users SET password_hash = %s, recovery_code = %s, recovery_code_used = false WHERE id = %s',
            [pw_hash, new_recovery_code, user_id]
        )
        send_discord_notification(
            DISCORD_WEBHOOK_AUTH, '🔄 パスワード再設定', f'ユーザー **{user_id}** のパスワードが再設定されました。', 0xFFA500,
            [{'name': '🆕 新しい復元コード', 'value': f'`{new_recovery_code}`', 'inline': False},
             {'name': '⚠️ 注意', 'value': 'このコードは使い捨てです。次回再設定時に再発行されます。', 'inline': False}]
        )
        return jsonify({'success': True, 'message': 'パスワードを再設定しました。', 'newRecoveryCode': new_recovery_code})
    except Exception as err:
        print(err)
        return jsonify({'error': 'サーバーエラー'}), 500


# ===================== 復元コード再発行 =====================
@app.post('/api/renew-recovery')
def renew_recovery():
    body = request.get_json(silent=True) or {}
    user_id = body.get('id')
    if not user_id:
        return jsonify({'error': 'IDが必要です'}), 400
    try:
        if not db_query('SELECT * FROM users WHERE id = %s', [user_id]):
            return jsonify({'error': 'ユーザーが見つかりません'}), 404
        new_recovery_code = generate_recovery_code()
        db_execute('UPDATE users SET recovery_code = %s, recovery_code_used = false WHERE id = %s',
                   [new_recovery_code, user_id])
        send_discord_notification(
            DISCORD_WEBHOOK_AUTH, '🔑 復元コード再発行', f'ユーザー **{user_id}** の復元コードが再発行されました。', 0x9B59B6,
            [{'name': '🆕 新しい復元コード', 'value': f'`{new_recovery_code}`', 'inline': False}]
        )
        return jsonify({'success': True, 'recoveryCode': new_recovery_code})
    except Exception as err:
        print(err)
        return jsonify({'error': 'サーバーエラー'}), 500


# ===================== データ同期 =====================
@app.post('/api/sync')
def sync_post():
    token = get_token_from_request()
    if not token:
        return jsonify({'error': '認証が必要です'}), 401
    try:
        user_id = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
    except Exception:
        return jsonify({'error': '認証エラー'}), 401
    try:
        body = request.get_json(silent=True) or {}
        best_score = body.get('bestScore')
        coins = body.get('coins')
        skins = body.get('skins')
        equipped_skin = body.get('equippedSkin')
        quests = body.get('quests')
        mode = body.get('mode')
        play_time = body.get('playTime')

        update_fields = []
        values = []

        # 以前は「size===8のときだけ保存」という条件があり、8×8以外の盤面サイズで遊んだ
        # soft/baked/hardモードのスコアがサーバーに一切保存されないバグがあった。

# フロントから「これは実際にプレイした結果だよ」という印（fromPlay）が来る。
        # 管理者コマンドで入れた仮のスコアは、この印が来たときだけ上書きを許可する。
        from_play = bool(body.get('fromPlay'))
        if mode and best_score is not None:
            rows = db_query('SELECT best_scores, best_score, admin_forced_scores FROM users WHERE id = %s', [user_id])
            best_scores = (rows[0]['best_scores'] if rows else None) or {}
            forced = (rows[0].get('admin_forced_scores') if rows else None) or {}
            if from_play and forced.get(mode):
                # 管理者が /setscore で入れた値は「1回だけ有効」。
                # 実際のプレイ結果が届いたら、それを正として上書きし、フタを外す。
                best_scores[mode] = best_score
                forced.pop(mode, None)
                update_fields.append('best_scores = %s')
                values.append(json.dumps(best_scores))
                update_fields.append('admin_forced_scores = %s')
                values.append(json.dumps(forced))
            elif not best_scores.get(mode) or best_score > best_scores[mode]:
                best_scores[mode] = best_score
                update_fields.append('best_scores = %s')
                values.append(json.dumps(best_scores))
            if best_score > 0:
                current_best = (rows[0].get('best_score') if rows else 0) or 0
                if best_score > current_best:
                    update_fields.append('best_score = %s')
                    values.append(best_score)

        if coins is not None:
            update_fields.append('coins = %s')
            values.append(coins or 0)
        if skins is not None:
            existing_rows = db_query('SELECT skins FROM users WHERE id = %s', [user_id])
            existing_skins = json.loads((existing_rows[0]['skins'] if existing_rows else None) or '["default"]')
            safe_skins = [s for s in (skins if isinstance(skins, list) else [])
                          if s in existing_skins or s not in GACHA_SKIN_ID_SET]
            update_fields.append('skins = %s')
            values.append(json.dumps(safe_skins))
        if equipped_skin is not None:
            update_fields.append('equipped_skin = %s')
            values.append(equipped_skin or 'default')
        if quests is not None:
            update_fields.append('quests = %s')
            values.append(json.dumps(quests or []))
        if play_time is not None:
            update_fields.append('play_time = %s')
            values.append(play_time or 0)

        if not update_fields:
            rows = db_query('SELECT best_scores, coins FROM users WHERE id = %s', [user_id])
            return jsonify({'success': True,
                            'bestScores': (rows[0]['best_scores'] if rows else None) or {},
                            'coins': int((rows[0]['coins'] if rows else 0) or 0)})
        values.append(user_id)
        query = f"UPDATE users SET {', '.join(update_fields)} WHERE id = %s"
        db_execute(query, values)
        rows = db_query('SELECT best_scores, coins FROM users WHERE id = %s', [user_id])
        return jsonify({'success': True,
                        'bestScores': (rows[0]['best_scores'] if rows else None) or {},
                        'coins': int((rows[0]['coins'] if rows else 0) or 0)})
    except Exception as err:
        print(err)
        return jsonify({'error': '認証エラー'}), 401


@app.get('/api/sync')
def sync_get():
    token = get_token_from_request()
    if not token:
        return jsonify({'error': '認証が必要です'}), 401
    try:
        user_id = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
        # 1回の問い合わせで「オンライン時刻の更新(8秒以上空いたときだけ)」と「データ取得」を済ませる
        rows = db_query("""
            WITH touch AS (
                UPDATE users SET last_active = NOW()
                WHERE id = %s AND (last_active IS NULL OR last_active < NOW() - INTERVAL '8 seconds') RETURNING 1
            )
            SELECT best_score, best_scores, coins, play_time, skins, equipped_skin, quests FROM users WHERE id = %s
        """, [user_id, user_id])
        if not rows:
            return jsonify({'error': 'ユーザーが見つかりません'}), 404
        user = rows[0]
        return jsonify({
            'bestScore': user.get('best_score'), 'bestScores': user.get('best_scores') or {},
            'coins': int(user.get('coins') or 0), 'playTime': user.get('play_time') or 0,
            'skins': json.loads(user.get('skins') or '["default"]'),
            'equippedSkin': user.get('equipped_skin') or 'default',
            'quests': json.loads(user.get('quests') or '[]')
        })
    except Exception:
        return jsonify({'error': '認証エラー'}), 401


# ===================== ランキング =====================
RANK_CACHE_SEC = 5


def _rank_build(mode, rtype):
    if rtype == 'playtime':
        val, where, order = 'play_time', 'play_time IS NOT NULL AND play_time > 0', 'play_time'
    elif rtype == 'coins':
        val, where, order = 'coins', 'coins IS NOT NULL AND coins > 0', 'coins'
    else:
        val = f"(best_scores->>'{mode}')::bigint"
        where = f"best_scores->>'{mode}' IS NOT NULL AND best_scores->>'{mode}' != '0'"
        order = val
    rows = db_query(f"SELECT id, {val} AS value, equipped_title, COUNT(*) OVER() AS total FROM users "
                    f"WHERE {where} ORDER BY {order} DESC LIMIT 50")
    total = int(rows[0]['total']) if rows else 0
    top = [{'id': r['id'], 'value': int(r['value']), 'title': titles.view_of(r.get('equipped_title'), r['id'], ADMIN_USER_ID)}
           for r in rows]
    return {'top': top, 'totalUsers': total}


@app.get('/api/ranking')
def ranking():
    mode = request.args.get('mode', 'soft')
    rtype = request.args.get('type', 'score')
    try:
        if rtype not in ('playtime', 'coins'):
            rtype = 'score'
            if mode not in ['soft', 'baked', 'hard', 'extreme', 'tetris', 'timeattack']:
                mode = 'soft'
        data = ttl_cache(f'rank:{rtype}:{mode}', RANK_CACHE_SEC, lambda: _rank_build(mode, rtype))
        out = dict(data)
        # 自分の値も一緒に返す(以前はアプリが /api/sync をもう1回呼んでいて、表示が遅かった)
        me = get_user_id_optional()
        if me != 'ゲスト':
            col = {'playtime': 'play_time', 'coins': 'coins'}.get(rtype)
            if col:
                r = db_query(f'SELECT {col} AS v FROM users WHERE id = %s', [me])
                out['me'] = int((r[0]['v'] if r else 0) or 0)
            else:
                r = db_query("SELECT best_scores->>%s AS v FROM users WHERE id = %s", [mode, me])
                out['me'] = int((r[0]['v'] if r else 0) or 0)
        return jsonify(out)
    except Exception as err:
        print(err)
        return jsonify({'error': 'ランキング取得エラー'}), 500


# ===================== 🆕 ガチャ(限定スキン、低確率、天井あり) =====================
@app.get('/api/gacha/state')
def gacha_state():
    try:
        user_id = require_auth()
        rows = db_query('SELECT coins, gacha_state, skins FROM users WHERE id = %s', [user_id])
        if not rows:
            raise ApiError('ユーザーが見つかりません', 404)
        u = rows[0]
        gs = u.get('gacha_state') or {'totalPulls': 0, 'pityCounter': 0}
        return jsonify({
            'coins': int(u.get('coins') or 0), 'totalPulls': gs.get('totalPulls', 0),
            'pityCounter': gs.get('pityCounter', 0), 'pityThreshold': GACHA_PITY_THRESHOLD,
            'cost': GACHA_COST, 'freeTickets': gs.get('freeTickets', 0),
            'ratePerSkin': GACHA_RATE_PER_SKIN, 'rateTotal': round(GACHA_RATE_PER_SKIN * len(GACHA_SKIN_IDS), 4),
            'ownedSkins': json.loads(u.get('skins') or '["default"]')
        })
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 500


def _gacha_roll_once(gs, owned_skins):
    """1回分の抽選。gs と owned_skins をその場で更新し、結果dictを返す(コインの増減は呼び出し側)。"""
    gs['totalPulls'] = (gs.get('totalPulls') or 0) + 1
    gs['pityCounter'] = (gs.get('pityCounter') or 0) + 1

    roll = random.random()
    natural_win = roll < GACHA_RATE_PER_SKIN * len(GACHA_SKIN_IDS)
    pity_win = (not natural_win) and gs['pityCounter'] >= GACHA_PITY_THRESHOLD
    won_skin_id = random.choice(GACHA_SKIN_IDS) if (natural_win or pity_win) else None

    if not won_skin_id:
        return {'resultType': 'miss', 'skinId': None, 'coinsGained': GACHA_MISS_COINS, 'pityTriggered': False}

    gs['pityCounter'] = 0
    if won_skin_id in owned_skins:
        return {'resultType': 'duplicate', 'skinId': won_skin_id,
                'coinsGained': GACHA_DUPLICATE_COINS, 'pityTriggered': pity_win}
    owned_skins.append(won_skin_id)
    return {'resultType': 'win', 'skinId': won_skin_id, 'coinsGained': 0, 'pityTriggered': pity_win}


@app.post('/api/gacha/pull')
def gacha_pull():
    """body: {"count": 1 | 10}(省略時は1)。無料チケットを先に使い、足りない分をコインで支払う。
    10連は10回ぶん全額を払えるときだけ実行(途中で足りなくなることはない)。"""
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        try:
            count = int(body.get('count') or 1)
        except (TypeError, ValueError):
            count = 0
        if count not in (1, 10):
            raise ApiError('回数が不正です', 400)

        with db_transaction() as cur:
            cur.execute('SELECT coins, gacha_state, skins FROM users WHERE id = %s FOR UPDATE', [user_id])
            u = cur.fetchone()
            if not u:
                raise ApiError('ユーザーが見つかりません', 404)
            coins = int(u.get('coins') or 0)
            gs = u.get('gacha_state') or {'totalPulls': 0, 'pityCounter': 0}
            owned_skins = json.loads(u.get('skins') or '[\"default\"]')

            tickets = gs.get('freeTickets') or 0
            tickets_used = min(tickets, count)
            coins_needed = (count - tickets_used) * GACHA_COST
            if coins < coins_needed:
                raise ApiError('コインが足りません', 400)
            coins -= coins_needed
            gs['freeTickets'] = tickets - tickets_used

            results = []
            for i in range(count):
                r = _gacha_roll_once(gs, owned_skins)
                r['usedFreeTicket'] = i < tickets_used
                coins += r['coinsGained']
                results.append(r)

            cur.execute('UPDATE users SET coins=%s, gacha_state=%s, skins=%s WHERE id=%s',
                        [coins, json.dumps(gs), json.dumps(owned_skins), user_id])

        resp = {
            'count': count, 'results': results, 'spentCoins': coins_needed, 'ticketsUsed': tickets_used,
            'coins': coins, 'totalPulls': gs['totalPulls'], 'pityCounter': gs['pityCounter'],
            'pityThreshold': GACHA_PITY_THRESHOLD, 'freeTickets': gs.get('freeTickets', 0),
            'ownedSkins': owned_skins
        }
        if count == 1:
            resp.update(results[0])  # 旧フロントとの互換(resultType, skinId, ... を直下にも置く)
        return jsonify(resp)
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 500


# ===================== 🆕 バトルパス(経験値でレベルアップ、報酬はループ、上限なし) =====================
# ★ レベルがどれだけ大きくなっても(ギフトで大量XPをもらっても)重くならないようにしてある:
#    ・受取済みは「1〜upto まで全部受取済み」+「それ以外に個別に受け取ったレベル(extra)」で保存する
#    ・未受取の件数・合計は、10レベル周期の足し算で一瞬で計算する(1レベルずつ数えない)
#    ・画面に返す報酬一覧は、いちばん新しい BATTLEPASS_VIEW_LIMIT レベル分だけ
BATTLEPASS_VIEW_LIMIT = 30


def _bp_prefix_sums():
    coins, tickets = [0], [0]
    for i in range(1, BATTLEPASS_CYCLE_LENGTH + 1):
        r = battle_pass_reward_for_level(i)
        coins.append(coins[-1] + (r['amount'] if r['type'] == 'coins' else 0))
        tickets.append(tickets[-1] + (r['amount'] if r['type'] == 'gacha_ticket' else 0))
    return coins, tickets


_BP_PREFIX_COINS, _BP_PREFIX_TICKETS = _bp_prefix_sums()


def bp_reward_totals(lo, hi):
    """レベル lo〜hi(両端を含む)の報酬の合計 (コイン, チケット) を、ループ計算なしで返す"""
    if hi < lo:
        return 0, 0

    def upto(n):  # レベル 1〜n の合計
        full, rem = divmod(n, BATTLEPASS_CYCLE_LENGTH)
        return (full * _BP_PREFIX_COINS[-1] + _BP_PREFIX_COINS[rem],
                full * _BP_PREFIX_TICKETS[-1] + _BP_PREFIX_TICKETS[rem])

    c_hi, t_hi = upto(hi)
    c_lo, t_lo = upto(lo - 1)
    return c_hi - c_lo, t_hi - t_lo


def bp_level_of(xp):
    return int(xp or 0) // BATTLEPASS_XP_PER_LEVEL + 1


def bp_load_claimed(raw):
    """DBの値 → (upto, extra)。旧形式(受取済みレベルのリスト)も読める"""
    if isinstance(raw, dict):
        upto = int(raw.get('upto') or 0)
        extra = {int(x) for x in (raw.get('extra') or [])}
    else:
        upto, extra = 0, {int(x) for x in (raw or [])}
    return bp_normalize(upto, extra)


def bp_normalize(upto, extra):
    extra = {x for x in extra if x > upto}
    while (upto + 1) in extra:      # 連続している分は upto にまとめる
        upto += 1
        extra.discard(upto)
    return upto, extra


def bp_dump_claimed(upto, extra):
    return json.dumps({'upto': upto, 'extra': sorted(extra)})


def bp_is_claimed(lv, upto, extra):
    return lv <= upto or lv in extra


def bp_pending(level, upto, extra):
    """到達済み・未受取の (件数, コイン合計, チケット合計)"""
    count = max(0, level - upto)
    coins, tickets = bp_reward_totals(upto + 1, level)
    for lv in extra:
        if upto < lv <= level:
            count -= 1
            r = battle_pass_reward_for_level(lv)
            if r['type'] == 'coins':
                coins -= r['amount']
            elif r['type'] == 'gacha_ticket':
                tickets -= r['amount']
    return count, coins, tickets


@app.get('/api/battlepass/state')
def battlepass_state():
    try:
        user_id = require_auth()
        rows = db_query('SELECT battlepass_xp, battlepass_claimed FROM users WHERE id = %s', [user_id])
        if not rows:
            raise ApiError('ユーザーが見つかりません', 404)
        xp = int(rows[0].get('battlepass_xp') or 0)
        upto, extra = bp_load_claimed(rows[0].get('battlepass_claimed'))
        level = bp_level_of(xp)
        xp_into_level = xp % BATTLEPASS_XP_PER_LEVEL
        first = max(1, level - BATTLEPASS_VIEW_LIMIT + 1)
        rewards = [dict(battle_pass_reward_for_level(lv), claimed=bp_is_claimed(lv, upto, extra))
                   for lv in range(first, level + 1)]
        p_count, p_coins, p_tickets = bp_pending(level, upto, extra)
        return jsonify({'xp': xp, 'level': level, 'xpIntoLevel': xp_into_level,
                        'xpPerLevel': BATTLEPASS_XP_PER_LEVEL, 'rewards': rewards,
                        'pendingCount': p_count, 'pendingCoins': p_coins, 'pendingTickets': p_tickets})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 500


@app.post('/api/battlepass/addxp')
def battlepass_addxp():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        add_amount = max(0, min(5000, int(body.get('amount') or 0)))
        db_execute('UPDATE users SET battlepass_xp = battlepass_xp + %s WHERE id = %s', [add_amount, user_id])
        return jsonify({'ok': True})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 500


@app.post('/api/battlepass/claim')
def battlepass_claim():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        lv = int(body.get('level') or 0)
        if lv < 1:
            raise ApiError('レベルを指定してください', 400)
        with db_transaction() as cur:
            cur.execute('SELECT coins, battlepass_xp, battlepass_claimed, gacha_state '
                        'FROM users WHERE id = %s FOR UPDATE', [user_id])
            u = cur.fetchone()
            if not u:
                raise ApiError('ユーザーが見つかりません', 404)
            if lv > bp_level_of(u.get('battlepass_xp')):
                raise ApiError('まだそのレベルに到達していません', 400)
            upto, extra = bp_load_claimed(u.get('battlepass_claimed'))
            if bp_is_claimed(lv, upto, extra):
                raise ApiError('すでに受け取り済みです', 400)
            reward = battle_pass_reward_for_level(lv)
            coins = int(u.get('coins') or 0)
            gs = u.get('gacha_state') or {'totalPulls': 0, 'pityCounter': 0}
            free_tickets = gs.get('freeTickets') or 0
            if reward['type'] == 'coins':
                coins += reward['amount']
            elif reward['type'] == 'gacha_ticket':
                free_tickets += reward['amount']
                gs['freeTickets'] = free_tickets
            extra.add(lv)
            upto, extra = bp_normalize(upto, extra)
            cur.execute('UPDATE users SET coins=%s, battlepass_claimed=%s, gacha_state=%s WHERE id=%s',
                        [coins, bp_dump_claimed(upto, extra), json.dumps(gs), user_id])
        return jsonify({'ok': True, 'reward': reward, 'coins': coins, 'freeTickets': free_tickets})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 500


@app.post('/api/battlepass/claim-all')
def battlepass_claim_all():
    """到達済みで未受け取りの報酬を全部まとめて受け取る(何百万レベルぶんでも一瞬)。"""
    try:
        user_id = require_auth()
        with db_transaction() as cur:
            cur.execute('SELECT coins, battlepass_xp, battlepass_claimed, gacha_state '
                        'FROM users WHERE id = %s FOR UPDATE', [user_id])
            u = cur.fetchone()
            if not u:
                raise ApiError('ユーザーが見つかりません', 404)
            level = bp_level_of(u.get('battlepass_xp'))
            upto, extra = bp_load_claimed(u.get('battlepass_claimed'))
            count, total_coins, total_tickets = bp_pending(level, upto, extra)
            if count <= 0:
                raise ApiError('受け取れる報酬がありません', 400)

            coins = int(u.get('coins') or 0) + total_coins
            gs = u.get('gacha_state') or {'totalPulls': 0, 'pityCounter': 0}
            gs['freeTickets'] = (gs.get('freeTickets') or 0) + total_tickets
            cur.execute('UPDATE users SET coins=%s, battlepass_claimed=%s, gacha_state=%s WHERE id=%s',
                        [coins, bp_dump_claimed(level, set()), json.dumps(gs), user_id])

        return jsonify({'ok': True, 'claimedCount': count, 'totalCoins': total_coins,
                        'totalTickets': total_tickets, 'coins': coins, 'freeTickets': gs['freeTickets']})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 500


# ===================== 🆕 スケジュールイベント(自動報酬) =====================
def compute_next_trigger(e, now):
    next_dt = now.replace(second=0, microsecond=0)
    recurrence = e['recurrence']
    if recurrence == 'once':
        try:
            next_dt = next_dt.replace(year=e['event_year'], month=e['event_month'], day=e['event_day'],
                                       hour=e['hour_jst'], minute=e['minute_jst'] or 0)
        except Exception:
            return None
        return next_dt if next_dt > now else None
    if recurrence == 'daily':
        next_dt = next_dt.replace(hour=e['hour_jst'], minute=e['minute_jst'] or 0)
        if next_dt <= now:
            next_dt += timedelta(days=1)
        return next_dt
    if recurrence == 'weekly':
        next_dt = next_dt.replace(hour=e['hour_jst'], minute=e['minute_jst'] or 0)
        target_dow_js = e['event_day']
        current_dow_js = (now.weekday() + 1) % 7
        diff = (target_dow_js - current_dow_js + 7) % 7
        if diff == 0 and next_dt <= now:
            diff = 7
        next_dt += timedelta(days=diff)
        return next_dt
    if recurrence == 'monthly':
        try:
            next_dt = next_dt.replace(day=e['event_day'], hour=e['hour_jst'], minute=e['minute_jst'] or 0)
        except Exception:
            return None
        if next_dt <= now:
            if next_dt.month == 12:
                next_dt = next_dt.replace(year=next_dt.year + 1, month=1)
            else:
                next_dt = next_dt.replace(month=next_dt.month + 1)
        return next_dt
    return None


@app.get('/api/events/list')
def events_list():
    try:
        rows = ttl_cache('events:rows', 5, lambda: db_query('SELECT * FROM scheduled_events ORDER BY id'))
        now = jst_now()
        events = []
        for e in rows:
            next_dt = compute_next_trigger(e, now)
            if next_dt is None:
                continue
            events.append({
                'id': e['id'], 'recurrence': e['recurrence'], 'hourJst': e['hour_jst'],
                'minuteJst': e.get('minute_jst') or 0, 'eventYear': e.get('event_year'),
                'eventMonth': e.get('event_month'), 'eventDay': e.get('event_day'),
                'rankingMode': e['ranking_mode'], 'rankPosition': e['rank_position'],
                'rewardCoins': e['reward_coins'],
                'secondsUntilNext': round((next_dt - now).total_seconds())
            })
        return jsonify({'events': events})
    except Exception:
        return jsonify({'events': []})


def run_event_scheduler():
    try:
        now = jst_now()
        hour, minute = now.hour, now.minute
        date_key = jst_date_key(now)
        rows = db_query('SELECT * FROM scheduled_events WHERE hour_jst = %s AND minute_jst = %s', [hour, minute])
        for ev in rows:
            recurrence = ev['recurrence']
            should_fire = False
            if recurrence == 'once':
                should_fire = (not ev['last_triggered_date'] and ev.get('event_year') == now.year and
                               ev.get('event_month') == now.month and ev.get('event_day') == now.day)
            elif recurrence == 'daily':
                should_fire = ev['last_triggered_date'] != date_key
            elif recurrence == 'weekly':
                current_dow_js = (now.weekday() + 1) % 7
                should_fire = current_dow_js == ev.get('event_day') and ev['last_triggered_date'] != date_key
            elif recurrence == 'monthly':
                should_fire = now.day == ev.get('event_day') and ev['last_triggered_date'] != date_key
            if not should_fire:
                continue
            mode = ev['ranking_mode']
            rank_rows = db_query(f"""
                SELECT id FROM users
                WHERE best_scores->>'{mode}' IS NOT NULL AND best_scores->>'{mode}' != '0'
                ORDER BY (best_scores->>'{mode}')::bigint DESC LIMIT 1 OFFSET %s
            """, [ev['rank_position'] - 1])
            if rank_rows:
                target_id = rank_rows[0]['id']
                db_execute('UPDATE users SET coins = coins + %s WHERE id = %s', [ev['reward_coins'], target_id])
                print(f"🎁 イベント報酬配布: {target_id} に {ev['reward_coins']}コイン（{mode}ランキング{ev['rank_position']}位）")
            if recurrence == 'once':
                db_execute('DELETE FROM scheduled_events WHERE id = %s', [ev['id']])
                cache_drop('events:')
            else:
                db_execute('UPDATE scheduled_events SET last_triggered_date = %s WHERE id = %s', [date_key, ev['id']])
    except Exception as err:
        print(f'イベントスケジューラーエラー: {err}')


# ===================== 🆕 週替わりチャレンジ =====================
def get_weekly_challenge_week_key(now):
    d = now.replace(hour=0, minute=0, second=0, microsecond=0)
    d -= timedelta(days=(d.weekday() + 1) % 7)
    sunday_9am = d.replace(hour=9, minute=0)
    if now < sunday_9am:
        d -= timedelta(days=7)
    return jst_date_key(d)


def run_weekly_challenge_scheduler():
    try:
        now = jst_now()
        week_key = get_weekly_challenge_week_key(now)
        if db_query('SELECT * FROM weekly_challenge WHERE week_key = %s', [week_key]):
            return
        prev = db_query('SELECT * FROM weekly_challenge ORDER BY id DESC LIMIT 1')
        if prev:
            prev_week_key = prev[0]['week_key']
            top = db_query('SELECT user_id FROM weekly_challenge_scores WHERE week_key=%s ORDER BY score DESC LIMIT 1',
                           [prev_week_key])
            if top:
                db_execute('UPDATE users SET coins = coins + %s WHERE id = %s',
                           [WEEKLY_CHALLENGE_REWARD, top[0]['user_id']])
                print(f"🎁 週替わりチャレンジ報酬: {top[0]['user_id']} に{WEEKLY_CHALLENGE_REWARD}コイン")
        modes = ['soft', 'baked', 'hard', 'extreme']
        db_execute(
            'INSERT INTO weekly_challenge (week_key, mode, size) VALUES (%s, %s, %s) ON CONFLICT (week_key) DO NOTHING',
            [week_key, random.choice(modes), random.randint(5, 18)]
        )
        print(f'🎲 新しい週替わりチャレンジを生成しました ({week_key})')
    except Exception as err:
        print(f'週替わりチャレンジスケジューラーエラー: {err}')


@app.get('/api/weekly-challenge')
def weekly_challenge_get():
    try:
        now = jst_now()
        week_key = get_weekly_challenge_week_key(now)
        challenge_rows = db_query('SELECT * FROM weekly_challenge WHERE week_key = %s', [week_key])
        if not challenge_rows:
            return jsonify({'challenge': None})
        challenge = challenge_rows[0]
        ranking_rows = db_query(
            'SELECT user_id, score FROM weekly_challenge_scores WHERE week_key=%s ORDER BY score DESC LIMIT 10',
            [week_key]
        )
        my_score = None
        token = get_token_from_request()
        if token:
            try:
                my_id = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
                mine = db_query('SELECT score FROM weekly_challenge_scores WHERE week_key=%s AND user_id=%s',
                               [week_key, my_id])
                if mine:
                    my_score = mine[0]['score']
            except Exception:
                pass
        y, m, d = [int(p) for p in week_key.split('-')]
        next_reset = now.replace(year=y, month=m, day=d, hour=9, minute=0, second=0, microsecond=0) + timedelta(days=7)
        return jsonify({
            'challenge': {'weekKey': week_key, 'mode': challenge['mode'], 'size': challenge['size'],
                         'reward': WEEKLY_CHALLENGE_REWARD,
                         'secondsUntilReset': round((next_reset - now).total_seconds())},
            'ranking': [{'id': r['user_id'], 'score': r['score']} for r in ranking_rows],
            'myScore': my_score, 'hasPlayed': my_score is not None
        })
    except Exception as err:
        print(err)
        return jsonify({'error': '取得に失敗しました'}), 500


@app.post('/api/weekly-challenge/submit')
def weekly_challenge_submit():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        score = body.get('score')
        if not isinstance(score, (int, float)) or score < 0:
            raise ApiError('正しいスコアを送信してください', 400)
        now = jst_now()
        week_key = get_weekly_challenge_week_key(now)
        if not db_query('SELECT * FROM weekly_challenge WHERE week_key = %s', [week_key]):
            raise ApiError('現在挑戦できるチャレンジがありません', 400)
        if db_query('SELECT * FROM weekly_challenge_scores WHERE week_key=%s AND user_id=%s', [week_key, user_id]):
            raise ApiError('今週はすでに挑戦済みです（1人1回まで）', 400)
        db_execute('INSERT INTO weekly_challenge_scores (week_key, user_id, score) VALUES (%s, %s, %s)',
                   [week_key, user_id, score])
        return jsonify({'ok': True})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        if 'unique' in str(err).lower() or 'duplicate' in str(err).lower():
            return jsonify({'error': '今週はすでに挑戦済みです（1人1回まで）'}), 400
        return jsonify({'error': '送信に失敗しました'}), 500


# ===================== お知らせ(管理者コマンド /announce の配信を表示) =====================
@app.get('/api/announcements/latest')
def announcements_latest():
    try:
        rows = ttl_cache('ann:latest', 10, lambda: db_query('SELECT id, message, created_at FROM announcements ORDER BY id DESC LIMIT 1'))
        if not rows:
            return jsonify({'announcement': None})
        return jsonify({'announcement': rows[0]})
    except Exception:
        return jsonify({'announcement': None})


# ===================== 🆕 ご要望・不具合報告(Discordへ転送) =====================
@app.post('/api/feedback/request')
def feedback_request():
    body = request.get_json(silent=True) or {}
    message = (body.get('message') or '').strip()
    if not message:
        return jsonify({'error': 'メッセージを入力してください'}), 400
    user_id = get_user_id_optional()
    try:
        sent = send_discord_notification(
            DISCORD_WEBHOOK_FEATURE_REQUEST, '💡 新しいご要望', to_copy_block(message), 0xFFD93D,
            [{'name': '送信者', 'value': user_id, 'inline': True}], wait=True
        )
        if not sent:
            return jsonify({'error': 'Discordへの送信に失敗しました。時間をおいて再度お試しください。'}), 502
        return jsonify({'ok': True})
    except Exception as err:
        print('要望送信エラー:', err)
        return jsonify({'error': '送信に失敗しました'}), 500


@app.post('/api/feedback/report')
def feedback_report():
    body = request.get_json(silent=True) or {}
    message = (body.get('message') or '').strip()
    image_base64 = body.get('imageBase64')
    if not message:
        return jsonify({'error': 'メッセージを入力してください'}), 400
    user_id = get_user_id_optional()
    try:
        image_bytes = None
        image_mime_type = 'image/png'
        image_ext = 'png'
        if image_base64:
            match = re.match(r'^data:(image/(\w+));base64,(.+)$', image_base64)
            if match:
                image_mime_type = match.group(1)
                image_ext = 'jpg' if match.group(2) == 'jpeg' else match.group(2)
                base64_data = match.group(3)
            else:
                base64_data = re.sub(r'^data:image/\w+;base64,', '', image_base64)
            image_bytes = base64.b64decode(base64_data)
            if len(image_bytes) > 8 * 1024 * 1024:
                return jsonify({'error': '画像サイズが大きすぎます（8MBまで）'}), 400
        sent = send_discord_notification(
            DISCORD_WEBHOOK_BUG_REPORT, '🐞 不具合の報告', to_copy_block(message), 0xFF5555,
            [{'name': '報告者', 'value': user_id, 'inline': True}],
            image_bytes, f'report.{image_ext}', image_mime_type, wait=True
        )
        if not sent:
            return jsonify({'error': 'Discordへの送信に失敗しました。時間をおいて再度お試しください。'}), 502
        return jsonify({'ok': True})
    except Exception as err:
        print('報告送信エラー:', err)
        return jsonify({'error': '送信に失敗しました'}), 500


# ===================== クエスト管理API =====================
@app.get('/api/quests/progress')
def quests_progress():
    try:
        user_id = require_auth()
        rows = db_query('SELECT quest_progress FROM users WHERE id = %s', [user_id])
        return jsonify({'progress': (rows[0].get('quest_progress') if rows else None) or {}})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.post('/api/quests/update')
def quests_update():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        quest_id = body.get('questId')
        rows = db_query('SELECT quest_progress FROM users WHERE id = %s', [user_id])
        quest_progress = (rows[0].get('quest_progress') if rows else None) or {}
        quest_progress[quest_id] = {'progress': body.get('progress'), 'claimed': body.get('claimed') or False}
        db_execute('UPDATE users SET quest_progress = %s WHERE id = %s', [json.dumps(quest_progress), user_id])
        return jsonify({'success': True, 'questProgress': quest_progress})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.post('/api/quests/claim')
def quests_claim():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        quest_id = body.get('questId')
        reward = body.get('reward') or 0
        rows = db_query('SELECT quest_progress, coins FROM users WHERE id = %s', [user_id])
        quest_progress = (rows[0].get('quest_progress') if rows else None) or {}
        current_coins = int((rows[0].get('coins') if rows else 0) or 0)
        if (quest_progress.get(quest_id) or {}).get('claimed'):
            return jsonify({'error': 'このクエストは既に受け取り済みです'}), 400
        new_coins = current_coins + reward
        quest_progress[quest_id] = {'progress': (quest_progress.get(quest_id) or {}).get('progress', 0), 'claimed': True}
        db_execute('UPDATE users SET quest_progress = %s, coins = %s WHERE id = %s',
                   [json.dumps(quest_progress), new_coins, user_id])
        return jsonify({'success': True, 'coins': new_coins, 'questProgress': quest_progress})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


# ===================== アカウント削除 =====================
@app.delete('/api/account/delete')
def account_delete():
    try:
        user_id = require_auth()
        db_execute('DELETE FROM users WHERE id = %s', [user_id])
        return jsonify({'success': True})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


# ===================== 🆕 フレンドAPI =====================
@app.post('/api/friends/request')
def friends_request():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        to_id = body.get('toId')
        if not to_id or to_id == user_id:
            return jsonify({'error': '有効なユーザーIDを指定してください'}), 400
        if not db_query('SELECT id FROM users WHERE id = %s', [to_id]):
            return jsonify({'error': 'ユーザーが見つかりません'}), 404
        if are_friends(user_id, to_id):
            return jsonify({'error': '既にフレンドです'}), 400
        existing = db_query(
            'SELECT * FROM friend_requests WHERE (from_id=%s AND to_id=%s) OR (from_id=%s AND to_id=%s)',
            [user_id, to_id, to_id, user_id]
        )
        pending_opposite = next((r for r in existing if r['status'] == 'pending' and r['from_id'] == to_id and r['to_id'] == user_id), None)
        if pending_opposite:
            db_execute('DELETE FROM friend_requests WHERE id = %s', [pending_opposite['id']])
            db_execute('INSERT INTO friends (user_id, friend_id) VALUES (%s,%s),(%s,%s) ON CONFLICT DO NOTHING',
                       [user_id, to_id, to_id, user_id])
            return jsonify({'success': True, 'autoAccepted': True})
        pending_same = next((r for r in existing if r['status'] == 'pending' and r['from_id'] == user_id and r['to_id'] == to_id), None)
        if pending_same:
            return jsonify({'error': '既に申請済みです'}), 400
        db_execute("""
            INSERT INTO friend_requests (from_id, to_id, status) VALUES (%s,%s,'pending')
            ON CONFLICT (from_id, to_id) DO UPDATE SET status='pending', created_at=NOW()
        """, [user_id, to_id])
        return jsonify({'success': True})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 500


@app.post('/api/friends/respond')
def friends_respond():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        from_id = body.get('fromId')
        action = body.get('action')
        if not from_id or action not in ('accept', 'decline'):
            return jsonify({'error': '不正なリクエストです'}), 400
        if not db_query("SELECT * FROM friend_requests WHERE from_id=%s AND to_id=%s AND status='pending'", [from_id, user_id]):
            return jsonify({'error': '申請が見つかりません'}), 404
        db_execute('DELETE FROM friend_requests WHERE from_id=%s AND to_id=%s', [from_id, user_id])
        if action == 'accept':
            db_execute('INSERT INTO friends (user_id, friend_id) VALUES (%s,%s),(%s,%s) ON CONFLICT DO NOTHING',
                       [user_id, from_id, from_id, user_id])
        return jsonify({'success': True})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.post('/api/friends/cancel')
def friends_cancel():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        db_execute("DELETE FROM friend_requests WHERE from_id=%s AND to_id=%s AND status='pending'",
                   [user_id, body.get('toId')])
        return jsonify({'success': True})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.delete('/api/friends/remove')
def friends_remove():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        friend_id = body.get('friendId')
        db_execute('DELETE FROM friends WHERE (user_id=%s AND friend_id=%s) OR (user_id=%s AND friend_id=%s)',
                   [user_id, friend_id, friend_id, user_id])
        return jsonify({'success': True})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


FRIENDS_SQL = """
SELECT
 (SELECT COALESCE(json_agg(t ORDER BY t.id), '[]'::json) FROM (
    SELECT u.id, u.best_score, u.coins, u.equipped_title,
           (u.last_active IS NOT NULL AND u.last_active > NOW() - INTERVAL '20 seconds') AS online
    FROM friends f JOIN users u ON u.id = f.friend_id WHERE f.user_id = %s) t) AS friends,
 (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
    SELECT from_id AS id, created_at FROM friend_requests WHERE to_id = %s AND status = 'pending' ORDER BY created_at DESC) t) AS incoming,
 (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
    SELECT to_id AS id, created_at FROM friend_requests WHERE from_id = %s AND status = 'pending' ORDER BY created_at DESC) t) AS outgoing
"""


def _friends_payload(user_id):
    r = db_query(FRIENDS_SQL, [user_id, user_id, user_id])[0]
    friends = r['friends'] or []
    for f in friends:
        f['title'] = titles.view_of(f.pop('equipped_title', None), f['id'], ADMIN_USER_ID)
    return {'friends': friends, 'incoming': r['incoming'] or [], 'outgoing': r['outgoing'] or []}


def _dm_unread_payload(user_id):
    rows = db_query('SELECT from_id, message FROM dm_messages WHERE to_id=%s AND is_read=FALSE '
                    'AND COALESCE(deleted, FALSE)=FALSE LIMIT 500', [user_id])
    mention_re = re.compile(r'@' + re.escape(user_id) + r'\b', re.IGNORECASE)
    per_friend = {}
    for r in rows:
        per_friend[r['from_id']] = per_friend.get(r['from_id'], 0) + 1
    return {'total': len(rows), 'mentions': sum(1 for r in rows if mention_re.search(r['message'] or '')),
            'perFriend': per_friend}


@app.get('/api/friends/list')
def friends_list():
    try:
        user_id = require_auth()
        return jsonify(_friends_payload(user_id))
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.get('/api/social/summary')
def social_summary():
    """フレンド一覧 + リクエスト + DM未読を1回でまとめて返す(裏の定期チェック用。以前は2回に分けていた)"""
    try:
        user_id = require_auth()
        out = _friends_payload(user_id)
        out['dm'] = _dm_unread_payload(user_id)
        return jsonify(out)
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


# ===================== 🆕 フレンド対決(デュエル) =====================
@app.post('/api/duels/challenge')
def duels_challenge():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        opponent_id = body.get('opponentId')
        if not opponent_id:
            raise ApiError('対戦相手を指定してください', 400)
        if not are_friends(user_id, opponent_id):
            raise ApiError('フレンドのみ対決できます', 403)
        game = body.get('game') if body.get('game') in ('candy', 'tetris') else 'candy'
        rows = db_query('INSERT INTO duels (challenger_id, opponent_id, game) VALUES (%s, %s, %s) RETURNING *',
                        [user_id, opponent_id, game])
        return jsonify({'duel': rows[0]})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 500


@app.post('/api/duels/<duel_id>/respond')
def duels_respond(duel_id):
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        accept = body.get('accept')
        rows = db_query('SELECT * FROM duels WHERE id=%s', [duel_id])
        if not rows:
            raise ApiError('対決が見つかりません', 404)
        if rows[0]['opponent_id'] != user_id:
            raise ApiError('権限がありません', 403)
        new_status = 'accepted' if accept else 'declined'
        db_execute('UPDATE duels SET status=%s WHERE id=%s', [new_status, duel_id])
        return jsonify({'ok': True, 'status': new_status})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.get('/api/duels/list')
def duels_list():
    try:
        user_id = require_auth()
        rows = db_query('SELECT * FROM duels WHERE challenger_id=%s OR opponent_id=%s ORDER BY created_at DESC LIMIT 30',
                        [user_id, user_id])
        return jsonify({'duels': rows})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.post('/api/duels/<duel_id>/submit-score')
def duels_submit_score(duel_id):
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        try:
            score = max(0, min(int(body.get('score') or 0), 2_000_000_000))
        except (TypeError, ValueError):
            raise ApiError('スコアが不正です', 400)
        rows = db_query('SELECT * FROM duels WHERE id=%s', [duel_id])
        if not rows:
            raise ApiError('対決が見つかりません', 404)
        duel = rows[0]
        if duel['challenger_id'] != user_id and duel['opponent_id'] != user_id:
            raise ApiError('権限がありません', 403)
        if duel['status'] not in ('accepted', 'completed'):
            raise ApiError('この対決はまだ受諾されていません', 400)
        column = 'challenger_score' if duel['challenger_id'] == user_id else 'opponent_score'
        db_execute(f'UPDATE duels SET {column} = %s WHERE id = %s', [score, duel_id])
        d = db_query('SELECT * FROM duels WHERE id=%s', [duel_id])[0]
        if d['challenger_score'] is not None and d['opponent_score'] is not None and d['status'] != 'completed':
            db_execute("UPDATE duels SET status='completed' WHERE id=%s", [duel_id])
            d['status'] = 'completed'
            WIN_REWARD, DRAW_REWARD = 200, 50
            if d['challenger_score'] > d['opponent_score']:
                db_execute('UPDATE users SET coins = coins + %s WHERE id = %s', [WIN_REWARD, d['challenger_id']])
            elif d['opponent_score'] > d['challenger_score']:
                db_execute('UPDATE users SET coins = coins + %s WHERE id = %s', [WIN_REWARD, d['opponent_id']])
            else:
                db_execute('UPDATE users SET coins = coins + %s WHERE id = %s', [DRAW_REWARD, d['challenger_id']])
                db_execute('UPDATE users SET coins = coins + %s WHERE id = %s', [DRAW_REWARD, d['opponent_id']])
        return jsonify({'duel': d})
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


# ===================== DM API =====================
@app.get('/api/dm/messages/<friend_id>')
def dm_messages(friend_id):
    try:
        user_id = require_auth()
        if not are_friends(user_id, friend_id):
            raise ApiError('フレンドのみDMできます', 403)
        rows = db_query("""
            SELECT id, from_id, to_id, CASE WHEN COALESCE(deleted, FALSE) THEN '' ELSE message END AS message, timestamp,
                   (edited_at IS NOT NULL) AS edited, COALESCE(deleted, FALSE) AS deleted FROM dm_messages
            WHERE (from_id=%s AND to_id=%s) OR (from_id=%s AND to_id=%s)
            ORDER BY timestamp DESC LIMIT 100
        """, [user_id, friend_id, friend_id, user_id])
        db_execute("UPDATE dm_messages SET is_read = TRUE WHERE from_id=%s AND to_id=%s AND is_read=FALSE",
                   [friend_id, user_id])
        return jsonify(list(reversed(rows)))
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.post('/api/dm/send')
def dm_send():
    try:
        user_id = require_auth()
        body = request.get_json(silent=True) or {}
        to_id = body.get('toId')
        message = body.get('message')
        if not message or not message.strip():
            raise ApiError('メッセージを入力してください', 400)
        if len(message) > 300:
            raise ApiError('メッセージは300文字以内にしてください', 400)
        if not are_friends(user_id, to_id):
            raise ApiError('フレンドのみDMできます', 403)
        rows = db_query(
            'INSERT INTO dm_messages (from_id, to_id, message) VALUES (%s,%s,%s) RETURNING id, from_id, to_id, message, timestamp',
            [user_id, to_id, message.strip()]
        )
        return jsonify(rows[0])
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


@app.get('/api/dm/unread-summary')
def dm_unread_summary():
    try:
        user_id = require_auth()
        return jsonify(_dm_unread_payload(user_id))
    except ApiError as e:
        return jsonify({'error': e.message}), e.status


# ===================== 管理者コマンド =====================
@app.post('/api/admin/command')
def admin_command():
    token = get_token_from_request()
    if not token:
        return jsonify({'error': '認証が必要です'}), 401
    try:
        user_id = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
    except Exception:
        return jsonify({'error': '認証エラー'}), 401
    if user_id != ADMIN_USER_ID:
        return jsonify({'error': '管理者権限がありません'}), 403

    body = request.get_json(silent=True) or {}
    command = body.get('command')
    if not command:
        return jsonify({'error': 'コマンドを入力してください'}), 400

    parts = command.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]
    result = ''

    def to_int(s):
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

    try:
        if cmd == '/setcoins':
            if len(args) < 2:
                raise ValueError('使用法: /setcoins <ユーザーID> <amount>')
            target_id = args[0]
            amount = to_int(args[1])
            if amount is None or amount < 0:
                raise ValueError('正しいコイン数を指定してください')
            if not db_query('SELECT id FROM users WHERE id = %s', [target_id]):
                raise ValueError(f'ユーザー {target_id} は見つかりません')
            db_execute('UPDATE users SET coins = %s WHERE id = %s', [amount, target_id])
            result = f'✅ {target_id} のコインを {amount} に設定しました。'

        elif cmd == '/addcoins':
            if len(args) < 2:
                raise ValueError('使用法: /addcoins <ユーザーID> <amount>')
            target_id = args[0]
            amount = to_int(args[1])
            if amount is None:
                raise ValueError('正しいコイン数を指定してください')
            rows = db_query('SELECT coins FROM users WHERE id = %s', [target_id])
            if not rows:
                raise ValueError(f'ユーザー {target_id} は見つかりません')
            new_total = max(0, int(rows[0].get('coins') or 0) + amount)
            db_execute('UPDATE users SET coins = %s WHERE id = %s', [new_total, target_id])
            result = f'✅ {target_id} のコインに {amount} を加算しました（合計: {new_total}）。'

        elif cmd == '/setscore':
            if len(args) < 3:
                raise ValueError('使用法: /setscore <ユーザーID> <mode> <score>')
            target_id, mode = args[0], args[1]
            score = to_int(args[2])
            valid_modes = ['soft', 'baked', 'hard', 'extreme', 'tetris', 'timeattack']
            if mode not in valid_modes:
                raise ValueError(f"モードは {', '.join(valid_modes)} のいずれかです")
            if score is None or score < 0:
                raise ValueError('正しいスコアを指定してください')
            rows = db_query('SELECT best_scores, admin_forced_scores FROM users WHERE id = %s', [target_id])
            if not rows:
                raise ValueError(f'ユーザー {target_id} は見つかりません')
            best_scores = rows[0].get('best_scores') or {}
            best_scores[mode] = score
            # 「管理者が入れた仮の値」に印を付ける。
            # 次にそのモードを実際にプレイしたら、この値は消えて本物の結果で上書きされる。
            forced = rows[0].get('admin_forced_scores') or {}
            forced[mode] = True
            regular_modes = ['soft', 'baked', 'hard', 'extreme']
            regular_scores = [best_scores[m] for m in regular_modes if isinstance(best_scores.get(m), (int, float))]
            max_score = max(regular_scores) if regular_scores else 0
            db_execute('UPDATE users SET best_scores = %s, best_score = %s, admin_forced_scores = %s WHERE id = %s',
                       [json.dumps(best_scores), max_score, json.dumps(forced), target_id])
            result = (f'✅ {target_id} の {mode}モードのベストスコアを {score} に設定しました。\n'
                      f'※ これは「1回だけ有効」な仮の値です。次に {mode} をプレイすると、その結果で上書きされます。')

        elif cmd == '/safety':
            if len(args) == 0:
                rows = db_query('SELECT admin_settings FROM users WHERE id = %s', [user_id])
                settings = (rows[0].get('admin_settings') if rows else None) or {'safetyMode': False}
                result = f"現在のセーフティモード: {'ON' if settings.get('safetyMode') else 'OFF'}"
            else:
                mode = args[0].lower()
                if mode not in ('on', 'off'):
                    raise ValueError('on または off を指定してください')
                safety_mode = mode == 'on'
                rows = db_query('SELECT admin_settings FROM users WHERE id = %s', [user_id])
                settings = (rows[0].get('admin_settings') if rows else None) or {'disabledBlocks': [], 'safetyMode': False}
                settings['safetyMode'] = safety_mode
                db_execute('UPDATE users SET admin_settings = %s WHERE id = %s', [json.dumps(settings), user_id])
                result = f'✅ 強制セーフティモードを {mode} に設定しました。'

        elif cmd == '/resetquests':
            db_execute('UPDATE users SET quest_progress = %s', [json.dumps({})])
            result = '✅ 全ユーザーのクエスト進捗をリセットしました。'

        elif cmd == '/setplaytime':
            time_val = to_int(args[0]) if args else None
            if time_val is None or time_val < 0:
                raise ValueError('正しいプレイ時間（秒）を指定してください')
            db_execute('UPDATE users SET play_time = %s WHERE id = %s', [time_val, user_id])
            result = f'✅ プレイ時間を {time_val}秒 に設定しました。'

        elif cmd == '/ban':
            target_id = args[0] if args else None
            if not target_id:
                raise ValueError('使用法: /ban <ユーザーID>')
            db_execute('UPDATE users SET banned = true WHERE id = %s', [target_id])
            result = f'✅ {target_id} をBANしました。'

        elif cmd == '/unban':
            target_id = args[0] if args else None
            if not target_id:
                raise ValueError('使用法: /unban <ユーザーID>')
            db_execute('UPDATE users SET banned = false WHERE id = %s', [target_id])
            result = f'✅ {target_id} のBANを解除しました。'

        elif cmd == '/resetuser':
            target_id = args[0] if args else None
            if not target_id:
                raise ValueError('使用法: /resetuser <ユーザーID>')
            db_execute("""
                UPDATE users SET best_score = 0, best_scores = '{}', coins = 0,
                    skins = '["default"]', quest_progress = '{}', play_time = 0
                WHERE id = %s
            """, [target_id])
            result = f'✅ {target_id} のデータをリセットしました。'

        elif cmd == '/listusers':
            rows = db_query('SELECT id, best_score, coins, play_time FROM users ORDER BY best_score DESC LIMIT 20')
            result = '📊 ユーザー一覧 (TOP20):\n' + '\n'.join(
                f"{u['id']}: {u['best_score']}点, {u['coins']}コイン, {u['play_time']}秒" for u in rows
            )

        elif cmd == '/search':
            target_id = args[0] if args else None
            if not target_id:
                raise ValueError('使用法: /search <ユーザーID>')
            rows = db_query('SELECT * FROM users WHERE id = %s', [target_id])
            if not rows:
                raise ValueError(f'ユーザー {target_id} は見つかりません')
            u = rows[0]
            created_str = u['created_at'].strftime('%Y/%m/%d %H:%M:%S') if u.get('created_at') else '不明'
            last_login_str = u['last_login'].strftime('%Y/%m/%d %H:%M:%S') if u.get('last_login') else 'なし'
            result = (f"🔍 ユーザー情報:\nID: {u['id']}\n🏆 ベストスコア: {u['best_score']}\n"
                     f"🪙 コイン: {u['coins']}\n⏱️ プレイ時間: {u.get('play_time') or 0}秒\n"
                     f"🚫 BAN: {'BAN中' if u.get('banned') else 'なし'}\n📅 作成日: {created_str}\n"
                     f"📅 最終ログイン: {last_login_str}")

        elif cmd == '/gift':
            if len(args) < 6:
                raise ValueError('使用法: /gift <ユーザーID|all> <コイン> <イベント通貨> <XP> <イベントXP> <メッセージ>\n'
                                 '例) /gift all 1000 0 0 0 お詫びのギフトです')
            amounts = [to_int(x) for x in args[1:5]]
            if None in amounts:
                raise ValueError('コイン・イベント通貨・XP・イベントXPは数字で指定してください')
            n, _batch = social.create_gift(db_query, user_id, args[0], ' '.join(args[5:]), *amounts)
            result = f'🎁 ギフトを {n}人 に送りました。受け取り側がログイン/ゲームオーバー後に開封します。'

        elif cmd == '/announce':
            message = ' '.join(args)
            if not message:
                raise ValueError('使用法: /announce <メッセージ>')
            db_execute('INSERT INTO announcements (message) VALUES (%s)', [message])
            cache_drop('ann:')
            result = f'📢 全ユーザーにお知らせを配信しました:\n「{message}」'

        elif cmd == '/discordtest':
            lines = [discord_status_text(), '', '🔎 テスト送信の結果:']
            for name, env in _DISCORD_HOOKS.items():
                url = globals().get(env)
                if not url:
                    lines.append(f'・{name}: ⚪ 未設定のため送っていません')
                    continue
                ok = send_discord_notification(url, '🔧 接続テスト', 'このメッセージが見えれば、このWebhookは正常です。', 0x00C2A8, wait=True)
                lines.append(f'・{name}: ' + ('✅ 送信できました' if ok else f"❌ 失敗 — {_discord_last_error.get(name, '原因不明')}"))
            result = '\n'.join(lines)

        elif cmd == '/stats':
            total_users = db_query('SELECT COUNT(*) AS c FROM users')[0]['c']
            total_score = db_query('SELECT SUM(best_score) AS s FROM users')[0]['s'] or 0
            total_coins = db_query('SELECT SUM(coins) AS s FROM users')[0]['s'] or 0
            total_playtime = db_query('SELECT SUM(play_time) AS s FROM users')[0]['s'] or 0
            result = (f'📊 サーバー統計:\n👤 総ユーザー数: {total_users}\n🏆 総スコア: {total_score}\n'
                     f'🪙 総コイン: {total_coins}\n⏱️ 総プレイ時間: {total_playtime}秒')

        elif cmd == '/eventadd':
            if len(args) < 9:
                raise ValueError(
                    '使用法: /eventadd <once|daily|weekly|monthly> <mode> <順位> <コイン> <年> <月> <日> <時> <分>\n'
                    '例1) 一度だけ: /eventadd once baked 1 500 2026 12 25 20 0\n'
                    '例2) 毎日: /eventadd daily baked 1 500 0 0 0 20 0\n'
                    '例3) 毎週(3=水曜): /eventadd weekly baked 1 500 0 0 3 20 0\n'
                    '例4) 毎月(15日): /eventadd monthly baked 1 500 0 0 15 20 0'
                )
            recurrence = args[0].lower()
            ev_mode = args[1]
            rank_pos = to_int(args[2])
            reward_coins = to_int(args[3])
            ev_year, ev_month, ev_day = to_int(args[4]), to_int(args[5]), to_int(args[6])
            hour_jst, minute_jst = to_int(args[7]), to_int(args[8])
            valid_modes2 = ['soft', 'baked', 'hard', 'extreme', 'tetris', 'timeattack']
            valid_recurrence = ['once', 'daily', 'weekly', 'monthly']
            if recurrence not in valid_recurrence:
                raise ValueError(f"繰り返しは {', '.join(valid_recurrence)} のいずれかです")
            if ev_mode not in valid_modes2:
                raise ValueError(f"モードは {', '.join(valid_modes2)} のいずれかです")
            if rank_pos is None or rank_pos < 1:
                raise ValueError('順位は1以上で指定してください')
            if reward_coins is None:
                raise ValueError('コイン数を指定してください')
            if hour_jst is None or hour_jst < 0 or hour_jst > 23:
                raise ValueError('時間は0〜23で指定してください（日本時間）')
            if minute_jst is None or minute_jst < 0 or minute_jst > 59:
                raise ValueError('分は0〜59で指定してください')
            if recurrence == 'once' and (ev_year is None or ev_month is None or ev_day is None):
                raise ValueError('onceの場合は年・月・日を正しく指定してください')
            if recurrence == 'weekly' and (ev_day is None or ev_day < 0 or ev_day > 6):
                raise ValueError('weeklyの場合は「日」を曜日(0=日〜6=土)で指定してください')
            if recurrence == 'monthly' and (ev_day is None or ev_day < 1 or ev_day > 31):
                raise ValueError('monthlyの場合は「日」を日付(1〜31)で指定してください')
            rows = db_query("""
                INSERT INTO scheduled_events (recurrence, ranking_mode, rank_position, reward_coins, event_year, event_month, event_day, hour_jst, minute_jst)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """, [recurrence, ev_mode, rank_pos, reward_coins, ev_year or None, ev_month or None, ev_day if ev_day is not None else None, hour_jst, minute_jst])
            cache_drop('events:')
            result = f'✅ イベントを追加しました（ID: {rows[0]["id"]}）: [{recurrence}] 「{ev_mode}」ランキング{rank_pos}位へ{reward_coins}コインを自動配布します。'

        elif cmd == '/eventlist':
            rows = db_query('SELECT * FROM scheduled_events ORDER BY id')
            if not rows:
                result = '登録されているイベントはありません。'
            else:
                lines = []
                wdays = ['日', '月', '火', '水', '木', '金', '土']
                for e in rows:
                    time_str = f"{e['hour_jst']:02d}:{(e.get('minute_jst') or 0):02d}"
                    if e['recurrence'] == 'once':
                        when_str = f"{e['event_year']}/{e['event_month']}/{e['event_day']} {time_str}に一度だけ"
                    elif e['recurrence'] == 'daily':
                        when_str = f'毎日{time_str}'
                    elif e['recurrence'] == 'weekly':
                        when_str = f"毎週{wdays[e['event_day']]}曜 {time_str}"
                    elif e['recurrence'] == 'monthly':
                        when_str = f"毎月{e['event_day']}日 {time_str}"
                    else:
                        when_str = time_str
                    lines.append(f"ID{e['id']}: {when_str}(JST) 「{e['ranking_mode']}」{e['rank_position']}位 → {e['reward_coins']}コイン")
                result = '📅 登録済みイベント一覧:\n' + '\n'.join(lines)

        elif cmd == '/eventremove':
            if not args:
                raise ValueError('使用法: /eventremove <ID>')
            rows = db_query('DELETE FROM scheduled_events WHERE id=%s RETURNING *', [args[0]])
            cache_drop('events:')
            if not rows:
                raise ValueError(f'イベントID {args[0]} は見つかりません')
            result = f'✅ イベントID {args[0]} を削除しました。'

        else:
            result = """📋 使用可能なコマンド:
  /setcoins <ユーザーID> <amount> - 指定ユーザーのコインを設定
  /addcoins <ユーザーID> <amount> - 指定ユーザーのコインに加算（元のコイン+amount）
  /setscore <ユーザーID> <mode> <score> - 指定ユーザーのモード別スコア設定 (soft, baked, hard, extreme, tetris, timeattack)
  /safety [on|off] - 強制セーフティモード（引数なしで状態表示）
  /announce <メッセージ> - 全ユーザーにお知らせを配信
  /eventadd <once|daily|weekly|monthly> <mode> <順位> <コイン> <年> <月> <日> <時> <分> - ランキング順位者へコインを自動配布するイベントを追加(JST基準)
  /eventlist - 登録済みイベント一覧を表示
  /eventremove <ID> - イベントを削除
  /resetquests - 全ユーザーのクエスト進捗リセット
  /setplaytime <seconds> - プレイ時間を設定
  /ban <ID> - ユーザーをBAN
  /unban <ID> - BAN解除
  /resetuser <ID> - ユーザーデータリセット
  /listusers - ユーザー一覧表示
  /search <ID> - ユーザー情報検索
  /stats - サーバー統計情報
  /help - このヘルプ"""

        return jsonify({'success': True, 'result': result})
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as err:
        return jsonify({'error': str(err) or 'サーバーエラー'}), 400


# ===================== 管理者API（ブロック設定用） =====================
@app.get('/api/admin/block-settings')
def admin_block_settings():
    token = get_token_from_request()
    if not token:
        return jsonify({'error': '認証が必要です'}), 401
    try:
        user_id = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
        if user_id != ADMIN_USER_ID:
            return jsonify({'error': '管理者権限がありません'}), 403
        rows = db_query('SELECT admin_settings FROM users WHERE id = %s', [user_id])
        settings = (rows[0].get('admin_settings') if rows else None) or {'disabledBlocks': [], 'safetyMode': False}
        return jsonify(settings)
    except Exception:
        return jsonify({'error': '認証エラー'}), 401


@app.post('/api/admin/block-toggle')
def admin_block_toggle():
    token = get_token_from_request()
    if not token:
        return jsonify({'error': '認証が必要です'}), 401
    try:
        user_id = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
        if user_id != ADMIN_USER_ID:
            return jsonify({'error': '管理者権限がありません'}), 403
        body = request.get_json(silent=True) or {}
        block_index = body.get('blockIndex')
        enabled = body.get('enabled')
        rows = db_query('SELECT admin_settings FROM users WHERE id = %s', [user_id])
        settings = (rows[0].get('admin_settings') if rows else None) or {'disabledBlocks': [], 'safetyMode': False}
        if enabled:
            settings['disabledBlocks'] = [i for i in settings.get('disabledBlocks', []) if i != block_index]
        else:
            if block_index not in settings.get('disabledBlocks', []):
                settings.setdefault('disabledBlocks', []).append(block_index)
        db_execute('UPDATE users SET admin_settings = %s WHERE id = %s', [json.dumps(settings), user_id])
        return jsonify({'success': True, 'settings': settings})
    except Exception:
        return jsonify({'error': '認証エラー'}), 401


# ===================== 管理者には全スキンを無料で贈呈 =====================
# スキンIDの「正式な一覧」はフロントエンド(script.js の SKINS/GACHA_SKINS、および
# 模様入りスキンの自動生成)にしかない。そのため、通常のプロフィール同期(/api/profile)の
# safe_skins チェック(ガチャ限定スキンを勝手に自称できないようにする仕組み)を迂回せず、
# 管理者アカウント自身が「今のフロントエンドが把握している全スキンID」を送ってきた場合に
# 限って、そのIDだけをまとめて所持済みにする専用エンドポイント。
@app.post('/api/admin/grant-all-skins')
def admin_grant_all_skins():
    token = get_token_from_request()
    if not token:
        return jsonify({'error': '認証が必要です'}), 401
    try:
        user_id = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
        if user_id != ADMIN_USER_ID:
            return jsonify({'error': '管理者権限がありません'}), 403
        body = request.get_json(silent=True) or {}
        skin_ids = body.get('skinIds')
        if not isinstance(skin_ids, list) or not skin_ids:
            return jsonify({'error': 'skinIdsを指定してください'}), 400
        skin_ids = [s for s in skin_ids if isinstance(s, str)][:2000]  # 念のため上限を設ける

        rows = db_query('SELECT skins FROM users WHERE id = %s', [user_id])
        if not rows:
            return jsonify({'error': 'ユーザーが見つかりません'}), 404
        existing_skins = json.loads((rows[0].get('skins')) or '["default"]')
        merged = sorted(set(existing_skins) | set(skin_ids))
        db_execute('UPDATE users SET skins = %s WHERE id = %s', [json.dumps(merged), user_id])
        return jsonify({'success': True, 'skins': merged})
    except Exception:
        return jsonify({'error': '認証エラー'}), 401


# ===================== 🆕 チャットAPI =====================
CHAT_CACHE_SEC = 1.5


def _chat_load():
    rows = db_query("SELECT c.id, c.user_id, CASE WHEN COALESCE(c.deleted, FALSE) THEN '' ELSE c.message END AS message, c.timestamp, "
                    "(c.edited_at IS NOT NULL) AS edited, COALESCE(c.deleted, FALSE) AS deleted, u.equipped_title "
                    "FROM (SELECT * FROM chat_messages ORDER BY id DESC LIMIT 50) c LEFT JOIN users u ON u.id = c.user_id ORDER BY c.id")
    for r in rows:
        r['title'] = titles.view_of(r.pop('equipped_title', None), r['user_id'], ADMIN_USER_ID)
    return rows


@app.get('/api/chat/messages')
def chat_messages():
    try:
        # 全員が数秒おきに同じ50件を取りに来るので、1.5秒だけ使い回す(送信・編集・取り消しですぐ作り直す)
        return jsonify(ttl_cache('chat:msgs', CHAT_CACHE_SEC, _chat_load))
    except Exception as err:
        print(err)
        return jsonify({'error': 'メッセージ取得エラー'}), 500


_chat_sends = 0


@app.post('/api/chat/send')
def chat_send():
    global _chat_sends
    token = get_token_from_request()
    if not token:
        return jsonify({'error': '認証が必要です'}), 401
    try:
        user_id = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])['id']
    except Exception:
        return jsonify({'error': '認証エラー'}), 401
    try:
        body = request.get_json(silent=True) or {}
        message = body.get('message')
        if not message or len(message.strip()) == 0:
            return jsonify({'error': 'メッセージを入力してください'}), 400
        if len(message) > 200:
            return jsonify({'error': 'メッセージは200文字以内にしてください'}), 400
        rows = db_query(
            'INSERT INTO chat_messages (user_id, message) VALUES (%s, %s) RETURNING id, user_id, message, timestamp',
            [user_id, message.strip()]
        )
        cache_drop('chat:')
        # 古いメッセージの掃除は、毎回ではなく20回に1回だけ(以前は送信のたびに全件を並べ替えていて遅かった)
        _chat_sends += 1
        if _chat_sends % 20 == 0:
            try:
                db_execute("DELETE FROM chat_messages WHERE id < (SELECT id FROM chat_messages ORDER BY id DESC OFFSET 49 LIMIT 1)")
            except Exception as err:
                print(f'chat prune error: {err}')
        return jsonify(rows[0])
    except Exception as err:
        print(err)
        return jsonify({'error': '送信に失敗しました'}), 500


# ===================== 🆕 プロフィールAPI =====================
@app.get('/api/user/profile/<user_id_param>')
def user_profile(user_id_param):
    try:
        rows = db_query('SELECT id, best_score, coins, play_time, created_at, equipped_title FROM users WHERE id = %s', [user_id_param])
        if not rows:
            return jsonify({'error': 'ユーザーが見つかりません'}), 404
        user = rows[0]
        return jsonify({
            'userId': user['id'], 'bestScore': user['best_score'], 'coins': int(user['coins'] or 0),
            'playTime': user.get('play_time') or 0,
            'title': titles.view_of(user.get('equipped_title'), user['id'], ADMIN_USER_ID),
            'joinedAt': user['created_at'].isoformat() if user.get('created_at') else None
        })
    except Exception as err:
        print(err)
        return jsonify({'error': 'プロフィール取得エラー'}), 500


# ===================== 🎃 ハロウィンイベントのAPIを登録 =====================
halloween.register(app, db_query, db_transaction, require_auth, ApiError)
import minigames  # 🕹 ミニゲームのランキング(別ファイル)
minigames.register(app, db_query, require_auth, ApiError)
social.register(app, db_query, db_transaction, require_auth, get_user_id_optional, ApiError, ADMIN_USER_ID,
                on_chat_change=lambda: cache_drop('chat:'))
titles.register(app, db_query, require_auth, ApiError, ADMIN_USER_ID)
engage.register(app, db_query, db_transaction, require_auth, ApiError)


# ===================== ヘルスチェック / スリープ対策 =====================
@app.get('/healthz')
def healthz():
    """外部の監視サービス・GitHub Actions・フロントエンドからの起こす用。?db=1 でDBにも軽く触れる
    (Supabaseの無料プランは1週間アクセスがないと一時停止されるため)。"""
    if request.args.get('db'):
        try:
            db_query('SELECT 1')
        except Exception as err:
            return jsonify({'ok': False, 'error': str(err)}), 503
    return jsonify({'ok': True})


# Render が自動で設定する RENDER_EXTERNAL_URL(または SELF_PING_URL)宛てに10分ごとに自分でアクセスして、
# 無料プランの「15分アクセスなしでスリープ」を防ぐ。
SELF_PING_URL = os.environ.get('SELF_PING_URL') or os.environ.get('RENDER_EXTERNAL_URL') or ''


def keep_alive_ping():
    if not SELF_PING_URL:
        return
    try:
        requests.get(SELF_PING_URL.rstrip('/') + '/healthz', timeout=20)
    except Exception as err:
        print(f'keep-alive ping 失敗: {err}')


# ===================== 起動処理 =====================
def start_app():
    try:
        init_db()
        print('✅ データベース初期化完了')
    except Exception as err:
        # テーブル作成に失敗しても、原因（大抵はDATABASE_URLかSSL設定）が
        # ログに残るようにしつつ、プロセス自体は落とさない
        print(f'❌ データベース初期化エラー: {err}')

    scheduler = BackgroundScheduler(timezone='UTC')
    scheduler.add_job(run_event_scheduler, 'interval', seconds=60, id='event_scheduler')
    scheduler.add_job(run_weekly_challenge_scheduler, 'interval', seconds=60, id='weekly_challenge_scheduler')
    if SELF_PING_URL:
        scheduler.add_job(keep_alive_ping, 'interval', minutes=10, id='keep_alive')
    scheduler.start()
    # 起動直後にも一度実行しておく(未生成の週替わりチャレンジをすぐ生成するため)
    run_weekly_challenge_scheduler()


start_app()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
