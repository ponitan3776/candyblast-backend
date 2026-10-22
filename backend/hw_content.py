# -*- coding: utf-8 -*-
"""
🎃 ハロウィン パート2 の「中身」(数字と文章だけ。ロジックは halloween.py)

数字を調整したいときは、ほとんどここだけ触ればOKです。
"""

# ===================== 📖 おばけ図鑑(24種) =====================
# id, 絵文字, 名前, レア度, 出やすさ(重み)
DEX = [
    ('chibi_pumpkin', '🎃', 'ちびかぼちゃ',       'common', 10),
    ('fuwa_ghost',    '👻', 'ふわゆうれい',       'common', 10),
    ('bat_kun',       '🦇', 'こうもりくん',       'common', 10),
    ('kumo_chi',      '🕷', 'くもっち',           'common', 10),
    ('kuro_neko',     '🐈‍⬛', 'くろねこ',         'common', 10),
    ('candy_fairy',   '🍬', 'キャンディようせい', 'common', 10),
    ('gaikotsu',      '💀', 'ガイコツ',           'common', 10),
    ('zombie',        '🧟', 'ゾンビ',             'common', 10),
    ('web_nushi',     '🕸', 'クモの巣のぬし',     'common', 10),
    ('tsuki_kobito',  '🌙', '月のこびと',         'common', 10),
    ('pero_obake',    '🍭', 'ペロペロおばけ',     'common', 10),
    ('hakamori',      '🪦', '墓守',               'common', 10),
    ('vampire_earl',  '🧛', 'ヴァンパイア伯爵',   'rare',    4),
    ('mini_witch',    '🧙', '見習い魔女',         'rare',    4),
    ('mummy',         '🩹', 'ほうたいミイラ',     'rare',    4),
    ('werewolf',      '🐺', 'オオカミおとこ',     'rare',    4),
    ('jester',        '🎭', '仮面の道化師',       'rare',    4),
    ('candle_boy',    '🕯', 'ろうそく小僧',       'rare',    4),
    ('owl_doctor',    '🦉', 'ふくろう博士',       'rare',    4),
    ('oni',           '👹', '赤鬼',               'epic',    1.5),
    ('night_dragon',  '🐉', 'ナイトドラゴン',     'epic',    1.5),
    ('crystal_seer',  '🔮', '水晶の占い師',       'epic',    1.5),
    ('lamp_genie',    '🧞', 'ランプの精',         'epic',    1.5),
    ('halloween_king', '👑', 'ハロウィンキング',  'legendary', 0.5),
]
DEX_IDS = [d[0] for d in DEX]
DEX_DROP_CHANCE = 0.30          # ゲーム1回ごとに、図鑑に新しい(または重複の)おばけが出会える確率
DEX_DUP_CANDIES = {'common': 3, 'rare': 8, 'epic': 20, 'legendary': 60}   # すでに出会っていたら、キャンディに変わる

# 図鑑の集めた数 → ごほうび
DEX_REWARDS = [
    {'need': 3,  'reward': {'type': 'coins', 'amount': 2000}},
    {'need': 6,  'reward': {'type': 'ticket', 'amount': 1}},
    {'need': 10, 'reward': {'type': 'bpxp', 'amount': 2500}},
    {'need': 15, 'reward': {'type': 'ticket', 'amount': 3}},
    {'need': 20, 'reward': {'type': 'coins', 'amount': 15000}},
    {'need': 24, 'reward': {'type': 'skin', 'skinId': 'hween_graveyardfog', 'extraCoins': 10000}},
]

# ===================== 🎪 ミニゲーム・スタンプラリー =====================
# 全ミニゲームを1回ずつ遊ぶとスタンプがたまる(ゲームの数は minigames.py の GAMES から自動)
STAMP_STEPS = [3, 6, 9, 12, 15, 'all']
STAMP_REWARDS = {
    3:     {'type': 'coins', 'amount': 1500},
    6:     {'type': 'ticket', 'amount': 1},
    9:     {'type': 'bpxp', 'amount': 2000},
    12:    {'type': 'ticket', 'amount': 2},
    15:    {'type': 'coins', 'amount': 8000},
    'all': {'type': 'skin', 'skinId': 'hween_trickortreat', 'extraCoins': 8000},
}

# ===================== 🚪 トリック・オア・トリート(3つのドア) =====================
TOT_FREE_PER_DAY = 1
TOT_EXTRA_COST = 30            # 2回目以降は、1回キャンディ30個
TOT_MAX_PER_DAY = 6
# kind, 重み, 内容, 見出し
TOT_TABLE = [
    ('candies', 30, {'candies': 25},   '🍬 キャンディがたっぷり！'),
    ('candies', 24, {'candies': 60},   '🍬🍬 大盛りキャンディ！'),
    ('candies', 8,  {'candies': 130},  '🍬🍬🍬 キャンディの山！'),
    ('coins',   14, {'coins': 1500},   '🪙 コインのおやつ'),
    ('coins',   5,  {'coins': 4000},   '🪙🪙 金貨の宝箱！'),
    ('ticket',  6,  {'tickets': 1},    '🎟 ハロウィンチケット！'),
    ('trick',   8,  {'candies': 5},    '🤡 いたずら！ …おわびにキャンディ少し'),
    ('dex',     4,  {},                '📖 おばけが遊びに来た！'),
    ('jackpot', 1,  {'tickets': 3, 'candies': 200}, '🎰 ジャックポット！！'),
]

# ===================== 🔮 きょうのおみくじ =====================
FORTUNES = [
    ('大吉', 10, 80, 'すべてがうまくいく夜。ガチャは今日がねらい目！'),
    ('中吉', 20, 50, 'いい風が吹いている。ミニゲームで高得点のチャンス'),
    ('吉',   30, 35, '穏やかな一日。コツコツ集めるのが吉'),
    ('小吉', 25, 25, '小さな幸せが見つかる日。ドアを開けてみて'),
    ('末吉', 10, 15, 'のんびり行こう。あせらないで'),
    ('凶',    5,  5, 'おっと、いたずらおばけが近くに…でも明日は上向き！'),
]
LUCKY_ITEMS = ['カボチャ', 'ほうき', 'ろうそく', 'クモの巣', 'キャンディ', '黒猫', '月', 'とんがり帽子', 'ランタン', 'マント']
LUCKY_COLORS = ['オレンジ', '紫', '黒', '緑', '赤', '金色', '銀色', '青']
FORTUNE_BONUS = 'おみくじ'

# ===================== 🌱 かぼちゃ畑(時間で育つ) =====================
PATCH_PLOTS = 4
# id, 名前, 絵文字, 育つ時間(秒), 収穫のキャンディ
PATCH_KINDS = [
    ('mini',   'ちびかぼちゃ',   '🎃', 30 * 60,      14),
    ('normal', 'かぼちゃ',       '🎃', 3 * 3600,     60),
    ('big',    'おおかぼちゃ',   '🎃', 8 * 3600,     170),
    ('gold',   'ゴールデン',     '🌟', 20 * 3600,    520),
]
PATCH_BY_ID = {k[0]: k for k in PATCH_KINDS}

# ===================== 👻 さまようおばけ =====================
GHOST_MAX_PER_DAY = 12
GHOST_COOLDOWN_SEC = 30
GHOST_NORMAL = (3, 10)         # キャンディの範囲
GHOST_GOLD_CHANCE = 0.08
GHOST_GOLD_CANDIES = 45

# ===================== 🌍 みんなでキャンディ大作戦 =====================
# 全員の累計キャンディの合計が、目標に届くたびに、全員に報酬(受け取りは1人ずつ)
COMMUNITY_GOALS = [
    {'need': 10_000,    'rewards': [{'type': 'coins', 'amount': 3000}]},
    {'need': 30_000,    'rewards': [{'type': 'coins', 'amount': 5000}, {'type': 'ticket', 'amount': 1}]},
    {'need': 80_000,    'rewards': [{'type': 'bpxp', 'amount': 3000}, {'type': 'ticket', 'amount': 2}]},
    {'need': 150_000,   'rewards': [{'type': 'coins', 'amount': 12000}, {'type': 'bpxp', 'amount': 4000}]},
    {'need': 300_000,   'rewards': [{'type': 'ticket', 'amount': 5}, {'type': 'coins', 'amount': 20000}]},
    {'need': 500_000,   'rewards': [{'type': 'ticket', 'amount': 8}, {'type': 'coins', 'amount': 50000}]},
]
COMMUNITY_CACHE_SEC = 30

# ===================== 🍬 キャンディのボーナスタイム =====================
# (JSTの月日ごと) 10/31 当日は3倍、前後は2倍。土日は1.5倍、毎晩20〜23時は1.5倍(いちばん高いものが1つだけ効く)
BOOST_DAYS = {(10, 31): (3.0, 'ハロウィン当日 キャンディ3倍！'),
              (10, 30): (2.0, 'ハロウィン前夜祭 キャンディ2倍！'),
              (11, 1):  (2.0, 'あとのまつり キャンディ2倍！')}
BOOST_WEEKEND = (1.5, '週末ボーナス キャンディ1.5倍')
BOOST_NIGHT = (1.5, 'ナイトタイム(20〜23時) キャンディ1.5倍')
MINI_GAME_BONUS = 4            # ミニゲームを1回遊ぶごとに、おまけで +4

# ===================== 👤 ゲストからの引き継ぎ =====================
GUEST_CARRYOVER_MAX = 150      # ゲストでためた🍬を、アカウント作成後に引き継げる上限(1回だけ)

# ===================== 📋 ミッション(パート2で増量) =====================
# stat: games / lines / score_max / tspin / vs_games / hw_pulls / duels / pc は既存、
#       mini_games(ミニゲーム回数) / mini_kinds(ちがう種類のミニゲーム) / tot / harvest / ghost / dex_new / fortune
DAILY_POOL = [
    {'id': 'd_games3',   'stat': 'games',     'target': 3,     'candies': 35, 'text': 'ゲームを3回プレイする'},
    {'id': 'd_games6',   'stat': 'games',     'target': 6,     'candies': 55, 'text': 'ゲームを6回プレイする'},
    {'id': 'd_games10',  'stat': 'games',     'target': 10,    'candies': 80, 'text': 'ゲームを10回プレイする'},
    {'id': 'd_lines20',  'stat': 'lines',     'target': 20,    'candies': 40, 'text': 'テトリスで合計20ライン消す'},
    {'id': 'd_lines60',  'stat': 'lines',     'target': 60,    'candies': 65, 'text': 'テトリスで合計60ライン消す'},
    {'id': 'd_score5k',  'stat': 'score_max', 'target': 5000,  'candies': 40, 'text': '1ゲームで5,000点を出す'},
    {'id': 'd_score20k', 'stat': 'score_max', 'target': 20000, 'candies': 65, 'text': '1ゲームで20,000点を出す'},
    {'id': 'd_tspin1',   'stat': 'tspin',     'target': 1,     'candies': 50, 'text': 'Tスピンを1回決める'},
    {'id': 'd_tspin3',   'stat': 'tspin',     'target': 3,     'candies': 70, 'text': 'Tスピンを3回決める'},
    {'id': 'd_vs1',      'stat': 'vs_games',  'target': 1,     'candies': 40, 'text': 'テトリスでAIと1回対戦する'},
    {'id': 'd_pulls3',   'stat': 'hw_pulls',  'target': 3,     'candies': 40, 'text': 'ハロウィンガチャを3回引く'},
    {'id': 'd_duel1',    'stat': 'duels',     'target': 1,     'candies': 55, 'text': 'フレンド対決を1回プレイする'},
    {'id': 'd_mini3',    'stat': 'mini_games', 'target': 3,    'candies': 45, 'text': 'ミニゲームを3回遊ぶ'},
    {'id': 'd_mini6',    'stat': 'mini_games', 'target': 6,    'candies': 70, 'text': 'ミニゲームを6回遊ぶ'},
    {'id': 'd_minik2',   'stat': 'mini_kinds', 'target': 2,    'candies': 45, 'text': '2種類のミニゲームを遊ぶ'},
    {'id': 'd_minik4',   'stat': 'mini_kinds', 'target': 4,    'candies': 75, 'text': '4種類のミニゲームを遊ぶ'},
    {'id': 'd_tot1',     'stat': 'tot',       'target': 1,     'candies': 30, 'text': 'トリック・オア・トリートのドアを開ける'},
    {'id': 'd_harvest1', 'stat': 'harvest',   'target': 1,     'candies': 30, 'text': 'かぼちゃ畑で収穫する'},
    {'id': 'd_harvest3', 'stat': 'harvest',   'target': 3,     'candies': 55, 'text': 'かぼちゃを3個収穫する'},
    {'id': 'd_ghost3',   'stat': 'ghost',     'target': 3,     'candies': 40, 'text': 'さまようおばけを3回つかまえる'},
    {'id': 'd_dex1',     'stat': 'dex_new',   'target': 1,     'candies': 50, 'text': '図鑑に新しいおばけを登録する'},
    {'id': 'd_fortune',  'stat': 'fortune',   'target': 1,     'candies': 25, 'text': 'きょうのおみくじを引く'},
]
WEEKLY_POOL = [
    {'id': 'w_games30',  'stat': 'games',     'target': 30,     'candies': 210, 'text': '今週ゲームを30回プレイする'},
    {'id': 'w_games60',  'stat': 'games',     'target': 60,     'candies': 330, 'text': '今週ゲームを60回プレイする'},
    {'id': 'w_lines300', 'stat': 'lines',     'target': 300,    'candies': 225, 'text': '今週テトリスで300ライン消す'},
    {'id': 'w_tspin10',  'stat': 'tspin',     'target': 10,     'candies': 240, 'text': '今週Tスピンを10回決める'},
    {'id': 'w_pc1',      'stat': 'pc',        'target': 1,      'candies': 280, 'text': '今週パーフェクトクリアを1回決める'},
    {'id': 'w_pulls20',  'stat': 'hw_pulls',  'target': 20,     'candies': 210, 'text': '今週ハロウィンガチャを20回引く'},
    {'id': 'w_score60k', 'stat': 'score_max', 'target': 60000,  'candies': 250, 'text': '今週1ゲームで60,000点を出す'},
    {'id': 'w_vs5',      'stat': 'vs_games',  'target': 5,      'candies': 195, 'text': '今週テトリスでAIと5回対戦する'},
    {'id': 'w_mini20',   'stat': 'mini_games', 'target': 20,    'candies': 240, 'text': '今週ミニゲームを20回遊ぶ'},
    {'id': 'w_minik6',   'stat': 'mini_kinds', 'target': 6,     'candies': 260, 'text': '今週6種類のミニゲームを遊ぶ'},
    {'id': 'w_tot5',     'stat': 'tot',       'target': 5,      'candies': 200, 'text': '今週トリック・オア・トリートを5回'},
    {'id': 'w_harvest10', 'stat': 'harvest',  'target': 10,     'candies': 230, 'text': '今週かぼちゃを10個収穫する'},
    {'id': 'w_ghost25',  'stat': 'ghost',     'target': 25,     'candies': 220, 'text': '今週さまようおばけを25回つかまえる'},
    {'id': 'w_dex4',     'stat': 'dex_new',   'target': 4,      'candies': 270, 'text': '今週おばけを4種類、図鑑に登録する'},
]
DAILY_COUNT = 4
WEEKLY_COUNT = 4

# ===================== 🛒 交換所(増量) =====================
SHOP = [
    {'id': 'coins',  'name': '🪙 5,000コイン', 'desc': 'すぐ使えるコイン', 'cost': 60, 'limit': 20,
     'give': {'type': 'coins', 'amount': 5000}},
    {'id': 'coins_big', 'name': '🪙🪙 30,000コイン', 'desc': 'まとめてドーンと', 'cost': 320, 'limit': 10,
     'give': {'type': 'coins', 'amount': 30000}},
    {'id': 'bpxp',   'name': '🎫 バトルパスXP +3,000', 'desc': 'バトルパスが一気に進む', 'cost': 70, 'limit': 10,
     'give': {'type': 'bpxp', 'amount': 3000}},
    {'id': 'bpxp_big', 'name': '🎫🎫 バトルパスXP +20,000', 'desc': 'パスを大きく進めたい人に', 'cost': 420, 'limit': 5,
     'give': {'type': 'bpxp', 'amount': 20000}},
    {'id': 'ticket', 'name': '🎟 ハロウィンチケット', 'desc': 'ガチャ1回ぶん。キャンディより少しお得', 'cost': 70, 'limit': 15,
     'give': {'type': 'ticket', 'amount': 1}},
    {'id': 'ticket5', 'name': '🎟×5 チケット5枚セット', 'desc': 'まとめ買いでお得', 'cost': 320, 'limit': 6,
     'give': {'type': 'ticket', 'amount': 5}},
    {'id': 'hween_spiderweb', 'name': '🕸 スパイダーウェブ(スキン)', 'desc': '交換所限定スキン', 'cost': 2000, 'limit': 1,
     'give': {'type': 'skin', 'skinId': 'hween_spiderweb'}},
    {'id': 'hween_batswarm', 'name': '🦇 バットスウォーム(スキン)', 'desc': '交換所限定スキン', 'cost': 3500, 'limit': 1,
     'give': {'type': 'skin', 'skinId': 'hween_batswarm'}},
    {'id': 'hween_frankenstein', 'name': '🧪 フランケンラボ(スキン)', 'desc': '交換所限定スキン・電気ビリビリの緑', 'cost': 2800, 'limit': 1,
     'give': {'type': 'skin', 'skinId': 'hween_frankenstein'}},
    {'id': 'hween_skeletondance', 'name': '💀 スケルトンダンス(スキン)', 'desc': '交換所限定のアニメーションスキン', 'cost': 4200, 'limit': 1,
     'give': {'type': 'skin', 'skinId': 'hween_skeletondance'}},
]

# ===================== 📆 ハロウィンのカウントダウン(10/31) =====================
HALLOWEEN_MONTH_DAY = (10, 31)
