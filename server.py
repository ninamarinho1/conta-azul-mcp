import os
import json
import httpx
from datetime import datetime
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.environ["CONTA_AZUL_CLIENT_ID"]
CLIENT_SECRET = os.environ["CONTA_AZUL_CLIENT_SECRET"]
REDIRECT_URI  = os.environ["CONTA_AZUL_REDIRECT_URI"]   # ex: https://SEU_APP.up.railway.app/callback
BASE_URL      = "https://api-v2.contaazul.com/v1"
AUTH_URL      = "https://auth.contaazul.com/oauth2"

# ── Token management ─────────────────────────────────────────────────────────
# O refresh_token é salvo em memória após o primeiro /callback.
# Depois de obter, adicione CONTA_AZUL_REFRESH_TOKEN como env var no Railway
# para sobreviver a reinicializações.
_token_cache: dict = {}

def _load_initial_tokens():
    rt = os.environ.get("CONTA_AZUL_REFRESH_TOKEN")
    if rt:
        _token_cache["refresh_token"] = rt
        _token_cache["expires_at"] = 0  # força refresh imediato

_load_initial_tokens()

async def _get_access_token() -> str:
    if not _token_cache.get("refresh_token"):
        raise RuntimeError("Não autorizado. Acesse /auth no navegador para autorizar.")

    if datetime.now().timestamp() < _token_cache.get("expires_at", 0):
        return _token_cache["access_token"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AUTH_URL}/token",
            data={
                "grant_type":    "refresh_token",
                "refresh_token": _token_cache["refresh_token"],
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        tokens = resp.json()

    _token_cache["access_token"]  = tokens["access_token"]
    _token_cache["refresh_token"] = tokens.get("refresh_token", _token_cache["refresh_token"])
    _token_cache["expires_at"]    = datetime.now().timestamp() + tokens.get("expires_in", 3600) - 120
    return _token_cache["access_token"]

async def _get(path: str, params: dict = None) -> dict:
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()

# ── MCP Server ────────────────────────────────────────────────────────────────
server = Server("conta-azul")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="buscar_lancamentos",
            description=(
                "Busca lançamentos financeiros (contas a pagar ou receber) por período. "
                "Retorna fornecedor, valor, categoria, centro de custo e status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_inicio": {"type": "string", "description": "Data inicial no formato YYYY-MM-DD"},
                    "data_fim":    {"type": "string", "description": "Data final no formato YYYY-MM-DD"},
                    "tipo":        {"type": "string", "enum": ["DESPESA", "RECEITA"], "default": "DESPESA"},
                    "status":      {"type": "string", "enum": ["PENDENTE", "QUITADO", "ATRASADO", "CANCELADO"], "description": "Opcional — filtra por status"},
                },
                "required": ["data_inicio", "data_fim"],
            },
        ),
        Tool(
            name="buscar_lancamentos_por_categoria",
            description=(
                "Busca lançamentos de despesas agrupados por categoria no período. "
                "Útil para análise de CC e diff semanal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_inicio": {"type": "string", "description": "YYYY-MM-DD"},
                    "data_fim":    {"type": "string", "description": "YYYY-MM-DD"},
                    "status":      {"type": "string", "description": "Opcional: PENDENTE, QUITADO, ATRASADO"},
                },
                "required": ["data_inicio", "data_fim"],
            },
        ),
        Tool(
            name="saldo_contas",
            description="Retorna todas as contas financeiras ativas e seus saldos atuais.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="listar_categorias",
            description="Lista todas as categorias financeiras (receita e despesa) cadastradas no Conta Azul.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["RECEITA", "DESPESA"], "description": "Opcional — filtra por tipo"},
                },
            },
        ),
        Tool(
            name="listar_centros_de_custo",
            description="Lista os centros de custo cadastrados.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=f"Erro: {e}")]

async def _dispatch(name: str, args: dict) -> dict:
    if name == "buscar_lancamentos":
        endpoint = "contas-a-pagar" if args.get("tipo", "DESPESA") == "DESPESA" else "contas-a-receber"
        params = {
            "pagina": 1,
            "tamanho_pagina": 200,
            "data_vencimento_de": args["data_inicio"],
            "data_vencimento_ate": args["data_fim"],
        }
        if args.get("status"):
            params["status"] = args["status"]
        return await _get(f"/financeiro/eventos-financeiros/{endpoint}/buscar", params)

    elif name == "buscar_lancamentos_por_categoria":
        params = {
            "pagina": 1,
            "tamanho_pagina": 200,
            "data_vencimento_de": args["data_inicio"],
            "data_vencimento_ate": args["data_fim"],
        }
        if args.get("status"):
            params["status"] = args["status"]
        data = await _get("/financeiro/eventos-financeiros/contas-a-pagar/buscar", params)

        # Agrupa por categoria
        por_categoria: dict[str, list] = {}
        items = data.get("items") or data.get("content") or data if isinstance(data, list) else []
        for item in items:
            parcelas = item.get("parcelas") or [item]
            for p in parcelas:
                cat = (p.get("categoria") or {}).get("nome") or "Sem categoria"
                por_categoria.setdefault(cat, []).append({
                    "descricao":    p.get("descricao") or item.get("descricao"),
                    "fornecedor":   (item.get("pessoa") or {}).get("nome"),
                    "valor":        p.get("valor"),
                    "valor_pago":   p.get("valor_pago"),
                    "status":       p.get("status"),
                    "vencimento":   p.get("data_vencimento"),
                    "pagamento":    p.get("data_pagamento"),
                })

        resumo = {cat: {"total": sum(i["valor"] or 0 for i in itens), "lancamentos": itens}
                  for cat, itens in sorted(por_categoria.items())}
        return resumo

    elif name == "saldo_contas":
        contas = await _get("/conta-financeira", {"pagina": 1, "tamanho_pagina": 20, "apenas_ativo": "true"})
        items = contas.get("items") or contas.get("content") or contas if isinstance(contas, list) else []
        resultado = []
        for c in items:
            cid = c.get("id")
            try:
                saldo = await _get(f"/conta-financeira/{cid}/saldo-atual")
            except Exception:
                saldo = {}
            resultado.append({
                "nome":  c.get("nome"),
                "tipo":  c.get("tipo"),
                "saldo": saldo.get("saldo_atual"),
            })
        return resultado

    elif name == "listar_categorias":
        params: dict = {"pagina": 1, "tamanho_pagina": 100}
        if args.get("tipo"):
            params["tipo"] = args["tipo"]
        return await _get("/categorias", params)

    elif name == "listar_centros_de_custo":
        return await _get("/centro-de-custo", {"pagina": 1, "tamanho_pagina": 100, "filtro_rapido": "ATIVO"})

    else:
        raise ValueError(f"Tool desconhecida: {name}")

# ── OAuth routes ──────────────────────────────────────────────────────────────
async def auth_redirect(request: Request):
    url = (
        f"{AUTH_URL}/authorize"
        f"?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=openid+profile+aws.cognito.signin.user.admin"
        f"&state=setup"
    )
    return RedirectResponse(url)

async def oauth_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h1>❌ Erro</h1><p>Código de autorização não recebido.</p>")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AUTH_URL}/token",
            data={
                "grant_type":   "authorization_code",
                "code":          code,
                "redirect_uri":  REDIRECT_URI,
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        tokens = resp.json()

    if "refresh_token" not in tokens:
        return HTMLResponse(f"<h1>❌ Erro na troca de token</h1><pre>{json.dumps(tokens, indent=2)}</pre>")

    _token_cache["access_token"]  = tokens["access_token"]
    _token_cache["refresh_token"] = tokens["refresh_token"]
    _token_cache["expires_at"]    = datetime.now().timestamp() + tokens.get("expires_in", 3600) - 120

    rt = tokens["refresh_token"]
    return HTMLResponse(f"""
    <h1>✅ Autorizado com sucesso!</h1>
    <p>Agora adicione esta variável de ambiente no Railway para que o token sobreviva a reinicializações:</p>
    <p><strong>Nome:</strong> <code>CONTA_AZUL_REFRESH_TOKEN</code></p>
    <p><strong>Valor:</strong></p>
    <textarea rows="3" cols="80" onclick="this.select()">{rt}</textarea>
    <p>Depois de adicionar, pode fechar essa janela. O MCP está pronto.</p>
    """)

async def health(request: Request):
    return HTMLResponse("ok")

# ── ASGI app ──────────────────────────────────────────────────────────────────
sse = SseServerTransport("/messages")

async def handle_sse(request: Request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())

app = Starlette(routes=[
    Route("/auth",     auth_redirect),
    Route("/callback", oauth_callback),
    Route("/health",   health),
    Route("/sse",      handle_sse),
    Mount("/messages", app=sse.handle_post_message),
])
