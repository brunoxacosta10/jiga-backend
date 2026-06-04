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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    app.run(host="0.0.0.0", port=port)
