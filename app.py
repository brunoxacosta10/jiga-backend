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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    app.run(host="0.0.0.0", port=port)
