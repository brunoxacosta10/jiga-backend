"""
Jigadores — backend ponte para os pontos do StreamElements.

O que faz:
- O site pede os pontos de um utilizador (pelo nome de Twitch).
- Este backend, que guarda o JWT secreto do StreamElements escondido,
  pergunta ao StreamElements e devolve o saldo.
- (Mais tarde) tira pontos quando se compra algo na loja, com validação.

Segurança:
- O JWT do StreamElements vive SÓ aqui, na variável de ambiente SE_JWT.
  Nunca no site. Quem tiver o JWT controla os pontos do canal todo.
- O CHANNEL_ID identifica o teu canal no StreamElements.
"""
import os
import urllib.request
import urllib.error
import json
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# Permite que o teu site (jigadores.com) chame este backend.
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://jigadores.com,https://www.jigadores.com"
).split(",")
CORS(app, origins=[o.strip() for o in ALLOWED_ORIGINS])

SE_JWT = os.environ.get("SE_JWT", "")            # chave secreta — só no Railway
CHANNEL_ID = os.environ.get("SE_CHANNEL_ID", "") # o teu Channel ID
SE_BASE = "https://api.streamelements.com/kappa/v2"

# Supabase — para verificar a identidade de quem joga (tokens de login)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gtvjvaenmtdatbmgcimr.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # publishable key

# Limites das apostas (segurança: evita apostas absurdas)
MIN_BET = int(os.environ.get("MIN_BET", "1"))
MAX_BET = int(os.environ.get("MAX_BET", "10000"))

# Contas a ESCONDER do ranking: bots e contas de sistema (sempre minúsculas).
# Podes acrescentar mais pelo Railway com a variável LEADERBOARD_HIDE
# (nomes separados por vírgula), além desta lista por defeito.
_HIDE_DEFAULT = {
    # a própria conta do canal e o bot do StreamElements
    "jigadores", "streamelements", "streamlabs",
    # bots de chat comuns
    "nightbot", "moobot", "fossabot", "wizebot", "botisimo", "coebot",
    "phantombot", "deepbot", "ankhbot", "vivbot", "soundalerts", "pretzelrocks",
    "sery_bot", "kofistreambot", "tangiabot", "blerp", "lumiastream", "streamstickers",
    # "viewers" automáticos / lurkers conhecidos
    "commanderroot", "anotherttvviewer", "stay_hydrated_bot", "streamfahrer",
}
_HIDE_EXTRA = {
    u.strip().lower() for u in os.environ.get("LEADERBOARD_HIDE", "").split(",") if u.strip()
}
LEADERBOARD_HIDE = _HIDE_DEFAULT | _HIDE_EXTRA


def verify_user(req):
    """Confirma a identidade de quem faz o pedido, via token da Supabase.
    Devolve o nome de Twitch (login) do utilizador, ou None se inválido."""
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        url = f"{SUPABASE_URL}/auth/v1/user"
        headers = {"Authorization": f"Bearer {token}", "apikey": SUPABASE_KEY}
        r = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(r, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        meta = data.get("user_metadata") or {}
        # o login da Twitch vem em nickname / preferred_username / name
        login = (meta.get("nickname") or meta.get("preferred_username")
                 or meta.get("user_name") or meta.get("name") or "")
        return login.lower() if login else None
    except Exception:
        return None


def get_user_points(username):
    """Lê os pontos atuais de um utilizador no StreamElements."""
    try:
        data = se_request(f"/points/{CHANNEL_ID}/{username}")
        return int(data.get("points", 0))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 0
        raise


def add_user_points(username, delta):
    """Adiciona (ou tira, se delta negativo) pontos a um utilizador.
    Usa o endpoint do StreamElements que soma ao saldo existente."""
    # endpoint: PUT /points/:channel/:user/:amount  (amount pode ser negativo)
    se_request(f"/points/{CHANNEL_ID}/{username}/{delta}", method="PUT")


def se_request(path, method="GET", body=None):
    """Faz um pedido à API do StreamElements com o JWT secreto."""
    url = f"{SE_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {SE_JWT}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


@app.route("/")
def home():
    ok = bool(SE_JWT) and bool(CHANNEL_ID)
    return jsonify({"service": "jigadores-backend", "configured": ok})


@app.route("/api/points/<username>")
def get_points(username):
    """Lê os pontos de um utilizador (pelo nome de Twitch). Só leitura."""
    if not SE_JWT or not CHANNEL_ID:
        return jsonify({"error": "backend não configurado"}), 500
    username = (username or "").strip().lower()
    if not username:
        return jsonify({"error": "username em falta"}), 400
    try:
        data = se_request(f"/points/{CHANNEL_ID}/{username}")
        # resposta típica: {"channel":..., "username":..., "points": N, "pointsAlltime": M, "rank": ...}
        return jsonify({
            "username": data.get("username", username),
            "points": data.get("points", 0),
            "points_alltime": data.get("pointsAlltime", 0),
            "rank": data.get("rank"),
        })
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # utilizador ainda sem pontos no canal
            return jsonify({"username": username, "points": 0, "points_alltime": 0, "rank": None})
        return jsonify({"error": f"streamelements erro {e.code}"}), 502
    except Exception as e:
        return jsonify({"error": "falha a contactar o streamelements"}), 502


@app.route("/api/leaderboard")
def leaderboard():
    """Top jogadores por pontos do canal (vem do leaderboard do StreamElements).
    Devolve uma lista [{name, score}] que o site usa para o pódio.
    O parâmetro ?period é aceite mas ignorado — o StreamElements devolve o
    leaderboard actual do canal (que o streamer pode configurar para resetar
    semanal/mensal nas definições de Loyalty)."""
    if not SE_JWT or not CHANNEL_ID:
        return jsonify({"error": "backend não configurado"}), 500
    # quantos lugares mostrar (o site usa top 3 no pódio + lista até 10)
    try:
        limit = int(request.args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 50))
    # buscamos MAIS ao StreamElements do que vamos mostrar, porque vamos
    # remover bots e contas de sistema — assim o top continua cheio de jogadores reais
    fetch_n = min(limit + len(LEADERBOARD_HIDE) + 30, 200)
    try:
        data = se_request(f"/points/{CHANNEL_ID}/top?limit={fetch_n}&offset=0")
        users = data.get("users", []) if isinstance(data, dict) else []
        board = []
        for u in users:
            name = (u.get("username", "") or "").strip()
            if not name or name.lower() in LEADERBOARD_HIDE:
                continue  # esconde bots / contas de sistema
            board.append({"name": name, "score": int(u.get("points", 0))})
            if len(board) >= limit:
                break
        return jsonify(board)
    except urllib.error.HTTPError as e:
        return jsonify({"error": f"streamelements erro {e.code}"}), 502
    except Exception:
        return jsonify({"error": "falha a contactar o streamelements"}), 502


import secrets

@app.route("/api/play/coinflip", methods=["POST"])
def play_coinflip():
    """Coinflip seguro. O resultado é decidido AQUI no servidor, não no site.
    Corpo: {"bet": 100, "choice": "heads"|"tails"}
    Cabeçalho: Authorization: Bearer <token da Supabase>
    """
    if not SE_JWT or not CHANNEL_ID:
        return jsonify({"error": "backend não configurado"}), 500

    # 1) confirma QUEM está a jogar (identidade via Supabase) — segurança
    username = verify_user(request)
    if not username:
        return jsonify({"error": "não autenticado"}), 401

    # 2) valida a aposta
    data = request.get_json(silent=True) or {}
    try:
        bet = int(data.get("bet", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "aposta inválida"}), 400
    choice = (data.get("choice") or "").lower()
    if choice not in ("heads", "tails"):
        return jsonify({"error": "escolhe cara (heads) ou coroa (tails)"}), 400
    if bet < MIN_BET or bet > MAX_BET:
        return jsonify({"error": f"aposta tem de ser entre {MIN_BET} e {MAX_BET}"}), 400

    # 3) confirma que a pessoa TEM mesmo os pontos (lê o saldo real)
    try:
        balance = get_user_points(username)
    except Exception:
        return jsonify({"error": "falha a ler o saldo"}), 502
    if bet > balance:
        return jsonify({"error": "não tens pontos suficientes", "balance": balance}), 400

    # 4) decide o resultado AQUI, à sorte, de forma justa (secrets = aleatório seguro)
    result = secrets.choice(["heads", "tails"])
    won = (result == choice)

    # 5) aplica o resultado nos pontos reais
    #    ganhou: +bet (ficou com o dobro da aposta no total)
    #    perdeu: -bet
    delta = bet if won else -bet
    try:
        add_user_points(username, delta)
        new_balance = balance + delta
    except Exception:
        return jsonify({"error": "falha a atualizar os pontos"}), 502

    return jsonify({
        "result": result,      # heads / tails que saiu
        "won": won,
        "delta": delta,        # quanto ganhou (+) ou perdeu (-)
        "balance": new_balance # saldo novo
    })


# --- Roleta europeia (0-36, um só zero) ---
RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

def roulette_payout(bet_type, bet_value, number):
    """Devolve o MULTIPLICADOR de ganho para uma aposta, dado o número que saiu.
    0 = perdeu. Caso contrário, é quantas vezes a aposta volta (além do reembolso).
    Ex: cor certa -> 1 (dobra); número exato -> 35."""
    if number == 0:
        # só ganha quem apostou exatamente no 0
        return 35 if (bet_type == "number" and bet_value == 0) else 0

    if bet_type == "number":
        return 35 if bet_value == number else 0
    if bet_type == "color":
        is_red = number in RED_NUMBERS
        if bet_value == "red":   return 1 if is_red else 0
        if bet_value == "black": return 1 if not is_red else 0
        return 0
    if bet_type == "parity":
        if bet_value == "even": return 1 if number % 2 == 0 else 0
        if bet_value == "odd":  return 1 if number % 2 == 1 else 0
        return 0
    if bet_type == "half":
        if bet_value == "low":  return 1 if 1 <= number <= 18 else 0   # 1-18
        if bet_value == "high": return 1 if 19 <= number <= 36 else 0  # 19-36
        return 0
    if bet_type == "dozen":
        if bet_value == "d1": return 2 if 1 <= number <= 12 else 0
        if bet_value == "d2": return 2 if 13 <= number <= 24 else 0
        if bet_value == "d3": return 2 if 25 <= number <= 36 else 0
        return 0
    if bet_type == "column":
        # colunas: 1ª =1,4,7...; 2ª =2,5,8...; 3ª =3,6,9...
        if bet_value == "c1": return 2 if number % 3 == 1 else 0
        if bet_value == "c2": return 2 if number % 3 == 2 else 0
        if bet_value == "c3": return 2 if number % 3 == 0 else 0
        return 0
    return 0


@app.route("/api/play/roulette", methods=["POST"])
def play_roulette():
    """Roleta segura. O número é decidido AQUI no servidor.
    Corpo: {"bet": 100, "bet_type": "color", "bet_value": "red"}
      bet_type: number|color|parity|half|dozen|column
      bet_value: number(0-36) | red/black | even/odd | low/high | d1/d2/d3 | c1/c2/c3
    Cabeçalho: Authorization: Bearer <token da Supabase>
    """
    if not SE_JWT or not CHANNEL_ID:
        return jsonify({"error": "backend não configurado"}), 500

    username = verify_user(request)
    if not username:
        return jsonify({"error": "não autenticado"}), 401

    data = request.get_json(silent=True) or {}
    try:
        bet = int(data.get("bet", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "aposta inválida"}), 400
    bet_type = (data.get("bet_type") or "").lower()
    bet_value = data.get("bet_value")

    # valida tipo e valor da aposta
    valid = {
        "number": None,  # validado em baixo (0-36)
        "color": {"red","black"},
        "parity": {"even","odd"},
        "half": {"low","high"},
        "dozen": {"d1","d2","d3"},
        "column": {"c1","c2","c3"},
    }
    if bet_type not in valid:
        return jsonify({"error": "tipo de aposta inválido"}), 400
    if bet_type == "number":
        try:
            bet_value = int(bet_value)
        except (TypeError, ValueError):
            return jsonify({"error": "número inválido"}), 400
        if bet_value < 0 or bet_value > 36:
            return jsonify({"error": "número tem de ser 0-36"}), 400
    else:
        bet_value = (str(bet_value) or "").lower()
        if bet_value not in valid[bet_type]:
            return jsonify({"error": "aposta inválida"}), 400

    if bet < MIN_BET or bet > MAX_BET:
        return jsonify({"error": f"aposta tem de ser entre {MIN_BET} e {MAX_BET}"}), 400

    try:
        balance = get_user_points(username)
    except Exception:
        return jsonify({"error": "falha a ler o saldo"}), 502
    if bet > balance:
        return jsonify({"error": "não tens pontos suficientes", "balance": balance}), 400

    # decide o número AQUI, à sorte (0-36)
    number = secrets.randbelow(37)
    mult = roulette_payout(bet_type, bet_value, number)
    won = mult > 0
    # ganhou: recebe bet*mult (a aposta fica com ele). perdeu: -bet
    delta = bet * mult if won else -bet

    try:
        add_user_points(username, delta)
        new_balance = balance + delta
    except Exception:
        return jsonify({"error": "falha a atualizar os pontos"}), 502

    return jsonify({
        "number": number,
        "color": ("green" if number == 0 else ("red" if number in RED_NUMBERS else "black")),
        "won": won,
        "delta": delta,
        "balance": new_balance
    })


@app.route("/api/play/roulette_multi", methods=["POST"])
def play_roulette_multi():
    """Roleta com várias apostas de uma vez (mesa realista).
    Corpo: {"bets": [{"bet_type":"color","bet_value":"red","bet":100}, ...]}
    Um único número é sorteado e aplicado a todas as apostas.
    """
    if not SE_JWT or not CHANNEL_ID:
        return jsonify({"error": "backend não configurado"}), 500

    username = verify_user(request)
    if not username:
        return jsonify({"error": "não autenticado"}), 401

    data = request.get_json(silent=True) or {}
    bet_list = data.get("bets")
    if not isinstance(bet_list, list) or not bet_list:
        return jsonify({"error": "sem apostas"}), 400
    if len(bet_list) > 50:
        return jsonify({"error": "demasiadas apostas"}), 400

    valid = {
        "number": None,
        "color": {"red", "black"},
        "parity": {"even", "odd"},
        "half": {"low", "high"},
        "dozen": {"d1", "d2", "d3"},
        "column": {"c1", "c2", "c3"},
    }

    # valida cada aposta e soma o total
    clean = []
    total = 0
    for b in bet_list:
        try:
            amt = int(b.get("bet", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "aposta inválida"}), 400
        bt = (b.get("bet_type") or "").lower()
        bv = b.get("bet_value")
        if bt not in valid:
            return jsonify({"error": "tipo de aposta inválido"}), 400
        if bt == "number":
            try:
                bv = int(bv)
            except (TypeError, ValueError):
                return jsonify({"error": "número inválido"}), 400
            if bv < 0 or bv > 36:
                return jsonify({"error": "número 0-36"}), 400
        else:
            bv = str(bv).lower()
            if bv not in valid[bt]:
                return jsonify({"error": "aposta inválida"}), 400
        if amt < 1:
            return jsonify({"error": "aposta tem de ser positiva"}), 400
        clean.append((bt, bv, amt))
        total += amt

    if total < MIN_BET or total > MAX_BET:
        return jsonify({"error": f"aposta total tem de ser entre {MIN_BET} e {MAX_BET}"}), 400

    # confirma saldo
    try:
        balance = get_user_points(username)
    except Exception:
        return jsonify({"error": "falha a ler o saldo"}), 502
    if total > balance:
        return jsonify({"error": "não tens pontos suficientes", "balance": balance}), 400

    # sorteia UM número e aplica a todas as apostas
    number = secrets.randbelow(37)
    gross_return = 0   # quanto volta ao jogador (ganhos + reembolso das que ganharam)
    for bt, bv, amt in clean:
        mult = roulette_payout(bt, bv, number)
        if mult > 0:
            gross_return += amt + amt * mult  # devolve aposta + ganho

    # delta = o que volta menos tudo o que apostou
    delta = gross_return - total
    try:
        if delta != 0:
            add_user_points(username, delta)
        new_balance = balance + delta
    except Exception:
        return jsonify({"error": "falha a atualizar os pontos"}), 502

    return jsonify({
        "number": number,
        "color": ("green" if number == 0 else ("red" if number in RED_NUMBERS else "black")),
        "delta": delta,
        "balance": new_balance
    })


# ====================== BLACKJACK ======================
# Estado dos jogos ativos, PERSISTIDO EM DISCO.
#
# Porquê disco e não só memória: o Railway reinicia o backend de vez em quando
# (deploys, sleep, crashes). Se o estado vivesse só em memória (dict), todos os
# jogos a meio desapareciam no restart -> o jogador carregava "Pedir carta" e
# levava "jogo inválido" porque o jogo já não existia. Guardando em disco,
# os jogos sobrevivem a restarts.
import time
import uuid
import json as _json
import threading

# ficheiro onde os jogos vivem (no Railway, /tmp é escrita garantida)
BJ_STORE_PATH = os.environ.get("BJ_STORE_PATH", "/tmp/bj_games.json")
BJ_TTL = 1800  # 30 min: jogos abandonados são limpos
_bj_lock = threading.Lock()  # evita corridas quando 2 pedidos chegam juntos


def _bj_load():
    """Lê todos os jogos do disco. Devolve dict {game_id: state}."""
    try:
        with open(BJ_STORE_PATH, "r") as f:
            return _json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _bj_save(games):
    """Grava todos os jogos no disco (escrita atómica para não corromper)."""
    tmp = BJ_STORE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            _json.dump(games, f)
        os.replace(tmp, BJ_STORE_PATH)  # troca atómica
    except Exception as e:
        print(f"[bj] erro a gravar jogos: {e}")


def _bj_get(game_id):
    """Lê um jogo específico do disco."""
    if not game_id:
        return None
    return _bj_load().get(game_id)


def _bj_put(state):
    """Grava/actualiza um jogo no disco (com lock para segurança)."""
    with _bj_lock:
        games = _bj_load()
        games[state["game_id"]] = state
        _bj_save(games)


def _bj_delete(game_id):
    """Remove um jogo do disco."""
    with _bj_lock:
        games = _bj_load()
        games.pop(game_id, None)
        _bj_save(games)


def _bj_cleanup():
    """Limpa jogos abandonados (mais de 30 min sem actividade)."""
    with _bj_lock:
        games = _bj_load()
        now = time.time()
        changed = False
        for gid in list(games.keys()):
            if now - games[gid].get("ts", now) > BJ_TTL:
                games.pop(gid, None)
                changed = True
        if changed:
            _bj_save(games)

def _new_deck():
    # 6 baralhos (como num casino), baralhados com aleatório seguro
    deck = []
    for _ in range(6):
        for r in ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]:
            for s in ["♠","♥","♦","♣"]:
                deck.append({"r": r, "s": s})
    # shuffle Fisher-Yates com secrets
    for i in range(len(deck)-1, 0, -1):
        j = secrets.randbelow(i+1)
        deck[i], deck[j] = deck[j], deck[i]
    return deck

def _draw(state):
    return state["deck"].pop()

def hand_value(cards):
    """Valor da mão. Ás = 11 ou 1 (o melhor possível). Devolve (valor, é_soft)."""
    total = 0
    aces = 0
    for c in cards:
        r = c["r"]
        if r == "A":
            total += 11; aces += 1
        elif r in ("K","Q","J","10"):
            total += 10
        else:
            total += int(r)
    soft = False
    while total > 21 and aces > 0:
        total -= 10; aces -= 1
    if aces > 0 and total <= 21:
        soft = True
    return total, soft

def is_blackjack(cards):
    return len(cards) == 2 and hand_value(cards)[0] == 21

def _public_state(state, reveal_dealer=False):
    """O que o site pode ver. As cartas do dealer ficam escondidas até ao fim."""
    hands = []
    for h in state["hands"]:
        v, soft = hand_value(h["cards"])
        hands.append({
            "cards": h["cards"], "value": v, "soft": soft,
            "bet": h["bet"], "done": h["done"], "result": h.get("result"),
            "is_bj": is_blackjack(h["cards"]) and len(state["hands"]) == 1,
        })
    if reveal_dealer:
        dv, _ = hand_value(state["dealer"])
        dealer = {"cards": state["dealer"], "value": dv}
    else:
        # mostra só a primeira carta do dealer
        dealer = {"cards": [state["dealer"][0], {"r":"?","s":"?"}], "value": None}
    return {
        "game_id": state["game_id"],
        "hands": hands,
        "active_hand": state["active"],
        "dealer": dealer,
        "status": state["status"],          # playing | done
        "can_double": state.get("can_double", False),
        "can_split": state.get("can_split", False),
        "can_insurance": state.get("can_insurance", False),
        "balance": state.get("balance"),
        "total_delta": state.get("total_delta"),
    }

def _settle(state):
    """Joga o dealer e calcula resultados de todas as mãos. Mexe nos pontos uma vez."""
    username = state["username"]
    # o dealer revela e pede até 17 (pára em 17, inclusive soft 17 fica — regra S17)
    dv, dsoft = hand_value(state["dealer"])
    # se ainda há mãos não rebentadas, o dealer joga
    any_alive = any(hand_value(h["cards"])[0] <= 21 for h in state["hands"])
    if any_alive:
        while True:
            dv, dsoft = hand_value(state["dealer"])
            if dv < 17:
                state["dealer"].append(_draw(state))
            else:
                break
    dv, _ = hand_value(state["dealer"])
    dealer_bj = is_blackjack(state["dealer"])

    total_return = 0  # quanto volta ao jogador (já tirámos as apostas no início)
    for h in state["hands"]:
        pv = hand_value(h["cards"])[0]
        bet = h["bet"]
        player_bj = is_blackjack(h["cards"]) and len(state["hands"]) == 1
        if pv > 21:
            h["result"] = "bust"          # perdeu (aposta já foi)
        elif player_bj and not dealer_bj:
            h["result"] = "blackjack"      # paga 3:2 -> devolve aposta + 1.5x
            total_return += bet + int(bet * 1.5)
        elif dealer_bj and not player_bj:
            h["result"] = "lose"
        elif dv > 21:
            h["result"] = "win"            # dealer rebentou
            total_return += bet * 2
        elif pv > dv:
            h["result"] = "win"
            total_return += bet * 2
        elif pv < dv:
            h["result"] = "lose"
        else:
            h["result"] = "push"           # empate: devolve a aposta
            total_return += bet
    # seguro (insurance): se o jogador pagou seguro e o dealer tem BJ, paga 2:1
    ins = state.get("insurance", 0)
    if ins > 0 and dealer_bj:
        total_return += ins * 3  # devolve o seguro + 2x

    # aplica nos pontos reais: já tínhamos tirado (apostas+seguro) no início.
    # agora devolvemos o que o jogador recupera.
    if total_return > 0:
        try:
            add_user_points(username, total_return)
        except Exception:
            pass
    # delta total desta partida = devolvido - tudo o que foi apostado
    staked = sum(h["bet"] for h in state["hands"]) + ins
    state["total_delta"] = total_return - staked
    state["status"] = "done"
    try:
        state["balance"] = get_user_points(username)
    except Exception:
        state["balance"] = None


@app.route("/api/play/blackjack/start", methods=["POST"])
def bj_start():
    if not SE_JWT or not CHANNEL_ID:
        return jsonify({"error": "backend não configurado"}), 500
    username = verify_user(request)
    if not username:
        return jsonify({"error": "não autenticado"}), 401
    _bj_cleanup()
    data = request.get_json(silent=True) or {}
    try:
        bet = int(data.get("bet", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "aposta inválida"}), 400
    if bet < MIN_BET or bet > MAX_BET:
        return jsonify({"error": f"aposta tem de ser entre {MIN_BET} e {MAX_BET}"}), 400
    try:
        balance = get_user_points(username)
    except Exception:
        return jsonify({"error": "falha a ler o saldo"}), 502
    if bet > balance:
        return jsonify({"error": "não tens pontos suficientes", "balance": balance}), 400

    # tira a aposta já (devolve-se no fim conforme o resultado)
    try:
        add_user_points(username, -bet)
    except Exception:
        return jsonify({"error": "falha a aplicar a aposta"}), 502

    deck = _new_deck()
    state = {
        "game_id": uuid.uuid4().hex,
        "username": username,
        "deck": deck,
        "hands": [{"cards": [], "bet": bet, "done": False, "result": None}],
        "dealer": [],
        "active": 0,
        "status": "playing",
        "insurance": 0,
        "ts": time.time(),
    }
    # reparte: jogador, dealer, jogador, dealer
    state["hands"][0]["cards"].append(_draw(state))
    state["dealer"].append(_draw(state))
    state["hands"][0]["cards"].append(_draw(state))
    state["dealer"].append(_draw(state))

    # opções disponíveis
    pc = state["hands"][0]["cards"]
    state["can_double"] = (balance - bet) >= bet
    state["can_split"] = (pc[0]["r"] == pc[1]["r"] or
                          (pc[0]["r"] in ("10","J","Q","K") and pc[1]["r"] in ("10","J","Q","K"))) and (balance - bet) >= bet
    state["can_insurance"] = state["dealer"][0]["r"] == "A" and (balance - bet) >= bet // 2

    # blackjack imediato? resolve já
    if is_blackjack(pc):
        _settle(state)
        state["status"] = "done"
        # jogo já acabou (blackjack natural) — nem vale a pena guardar, mas
        # guardamos por consistência e o cleanup trata depois
        return jsonify(_public_state(state, reveal_dealer=True))

    _bj_put(state)  # grava o jogo novo no disco
    return jsonify(_public_state(state))


def _get_game(req):
    data = req.get_json(silent=True) or {}
    gid = data.get("game_id")
    state = _bj_get(gid)
    return state, data

def _verify_owner(req, state):
    """Confirma que quem age é o dono do jogo.
    Devolve (ok, mensagem_de_erro). Mensagens distintas para diagnóstico:
    - 'não autenticado': o token da Supabase falhou
    - 'jogo não encontrado': o game_id não existe (expirou ou perdeu-se)
    - 'este jogo não é teu': o jogo existe mas é de outro utilizador
    """
    username = verify_user(req)
    if not username:
        return False, "não autenticado"
    if not state:
        return False, "jogo não encontrado"
    # comparação à prova de maiúsculas/espaços
    owner = (state.get("username") or "").strip().lower()
    if owner != username.strip().lower():
        return False, "este jogo não é teu"
    return True, None


@app.route("/api/play/blackjack/hit", methods=["POST"])
def bj_hit():
    state, _ = _get_game(request)
    ok, err = _verify_owner(request, state)
    if not ok:
        return jsonify({"error": err}), 400
    if state["status"] != "playing":
        return jsonify({"error": "jogo terminado"}), 400
    h = state["hands"][state["active"]]
    h["cards"].append(_draw(state))
    state["can_double"] = False
    state["can_split"] = False
    state["can_insurance"] = False
    state["ts"] = time.time()
    v = hand_value(h["cards"])[0]
    if v >= 21:
        h["done"] = True
        return _bj_advance(state)
    _bj_put(state)  # grava o estado actualizado
    return jsonify(_public_state(state))


@app.route("/api/play/blackjack/stand", methods=["POST"])
def bj_stand():
    state, _ = _get_game(request)
    ok, err = _verify_owner(request, state)
    if not ok:
        return jsonify({"error": err}), 400
    if state["status"] != "playing":
        return jsonify({"error": "jogo terminado"}), 400
    state["hands"][state["active"]]["done"] = True
    state["ts"] = time.time()
    return _bj_advance(state)


@app.route("/api/play/blackjack/double", methods=["POST"])
def bj_double():
    state, _ = _get_game(request)
    ok, err = _verify_owner(request, state)
    if not ok:
        return jsonify({"error": err}), 400
    if state["status"] != "playing":
        return jsonify({"error": "jogo terminado"}), 400
    h = state["hands"][state["active"]]
    if len(h["cards"]) != 2:
        return jsonify({"error": "só podes dobrar no início da mão"}), 400
    username = state["username"]
    bet = h["bet"]
    try:
        bal = get_user_points(username)
        if bal < bet:
            return jsonify({"error": "não tens pontos para dobrar"}), 400
        add_user_points(username, -bet)  # tira a aposta extra
    except Exception:
        return jsonify({"error": "falha a dobrar"}), 502
    h["bet"] += bet
    h["cards"].append(_draw(state))   # só mais uma carta
    h["done"] = True
    state["ts"] = time.time()
    return _bj_advance(state)


@app.route("/api/play/blackjack/split", methods=["POST"])
def bj_split():
    state, _ = _get_game(request)
    ok, err = _verify_owner(request, state)
    if not ok:
        return jsonify({"error": err}), 400
    if state["status"] != "playing":
        return jsonify({"error": "jogo terminado"}), 400
    if len(state["hands"]) != 1:
        return jsonify({"error": "só podes dividir uma vez"}), 400
    h = state["hands"][0]
    if len(h["cards"]) != 2:
        return jsonify({"error": "só podes dividir no início"}), 400
    c0, c1 = h["cards"]
    same = c0["r"] == c1["r"] or (c0["r"] in ("10","J","Q","K") and c1["r"] in ("10","J","Q","K"))
    if not same:
        return jsonify({"error": "só podes dividir cartas iguais"}), 400
    username = state["username"]
    bet = h["bet"]
    try:
        bal = get_user_points(username)
        if bal < bet:
            return jsonify({"error": "não tens pontos para dividir"}), 400
        add_user_points(username, -bet)  # segunda mão precisa de outra aposta
    except Exception:
        return jsonify({"error": "falha a dividir"}), 502
    # cria duas mãos
    state["hands"] = [
        {"cards": [c0, _draw(state)], "bet": bet, "done": False, "result": None},
        {"cards": [c1, _draw(state)], "bet": bet, "done": False, "result": None},
    ]
    state["active"] = 0
    state["can_double"] = True
    state["can_split"] = False
    state["can_insurance"] = False
    state["ts"] = time.time()
    _bj_put(state)  # grava o estado actualizado
    return jsonify(_public_state(state))


@app.route("/api/play/blackjack/insurance", methods=["POST"])
def bj_insurance():
    state, _ = _get_game(request)
    ok, err = _verify_owner(request, state)
    if not ok:
        return jsonify({"error": err}), 400
    if state["status"] != "playing" or not state.get("can_insurance"):
        return jsonify({"error": "seguro indisponível"}), 400
    username = state["username"]
    ins = state["hands"][0]["bet"] // 2
    try:
        bal = get_user_points(username)
        if bal < ins:
            return jsonify({"error": "sem pontos para seguro"}), 400
        add_user_points(username, -ins)
    except Exception:
        return jsonify({"error": "falha no seguro"}), 502
    state["insurance"] = ins
    state["can_insurance"] = False
    state["ts"] = time.time()
    _bj_put(state)  # grava o estado actualizado
    return jsonify(_public_state(state))


def _bj_advance(state):
    """Passa à mão seguinte; se não há mais, joga o dealer e resolve."""
    # procura a próxima mão não terminada
    n = len(state["hands"])
    nxt = state["active"]
    while nxt < n and state["hands"][nxt]["done"]:
        nxt += 1
    if nxt < n:
        state["active"] = nxt
        state["can_double"] = len(state["hands"][nxt]["cards"]) == 2
        _bj_put(state)  # grava o estado actualizado no disco
        return jsonify(_public_state(state))
    # todas as mãos terminadas -> dealer joga e resolve
    _settle(state)
    # jogo acabou: grava resultado final e depois remove (já não é preciso)
    state["status"] = "done"
    _bj_delete(state["game_id"])
    return jsonify(_public_state(state, reveal_dealer=True))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    app.run(host="0.0.0.0", port=port)