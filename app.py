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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    app.run(host="0.0.0.0", port=port)
