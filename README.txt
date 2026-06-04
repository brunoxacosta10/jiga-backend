JIGADORES — BACKEND (ponte para os pontos do StreamElements)
============================================================

O QUE É:
Um servidor que liga o site jigadores.com aos pontos do StreamElements,
guardando a chave secreta (JWT) escondida e em segurança.

PÔR NO RAILWAY:
1. Sobe esta pasta para um repositório GitHub novo (ex: jiga-backend).
2. No Railway: New Project -> Deploy from GitHub -> escolhe o repo.
3. Em Variables, define:
   - SE_JWT          = (o teu JWT token do StreamElements — SECRETO)
   - SE_CHANNEL_ID   = 5fffb03b13002b8e2bd5a5ab
   - ALLOWED_ORIGINS = https://jigadores.com,https://www.jigadores.com
4. O Railway arranca com o Procfile (gunicorn).
5. Gera um domínio (Settings -> Networking -> Generate Domain).

TESTAR:
Abre  https://<o-teu-backend>.up.railway.app/
Deve mostrar  {"service":"jigadores-backend","configured":true}

Depois:  https://<o-teu-backend>.up.railway.app/api/points/jigadores
Deve mostrar os pontos desse utilizador.

SEGURANÇA:
- O SE_JWT só vive aqui no Railway, nunca no site.
- Nunca partilhes o SE_JWT com ninguém.
