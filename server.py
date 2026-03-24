import os
import json
import calendar
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
REDIRECT_URI  = os.environ["CONTA_AZUL_REDIRECT_URI"]
BASE_URL      = "https://api-v2.contaazul.com/v1"
AUTH_URL      = "https://auth.contaazul.com/oauth2"

# ── Token management ─────────────────────────────────────────────────────────
_token_cache: dict = {}

def _load_initial_tokens():
    rt = os.environ.get("CONTA_AZUL_REFRESH_TOKEN")
    if rt:
        _token_cache["refresh_token"] = rt
        _token_cache["expires_at"] = 0  # força refresh imediato

_load_initial_tokens()

RENDER_API_KEY    = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = "srv-d6tcskfafjfc73ffao2g"

async def _save_refresh_token_to_render(token: str):
    """Atualiza apenas o CONTA_AZUL_REFRESH_TOKEN no Render, preservando todas as outras variáveis."""
    if not RENDER_API_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
                headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
            )
            existing = resp.json() if resp.status_code == 200 else []
            env_vars = [{"key": v["key"], "value": v["value"]} for v in existing if v.get("key") != "CONTA_AZUL_REFRESH_TOKEN"]
            env_vars.append({"key": "CONTA_AZUL_REFRESH_TOKEN", "value": token})
            await client.put(
                f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
                headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"},
                json=env_vars,
            )
    except Exception:
        pass  # falha silenciosa — não quebra o fluxo principal

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

    new_rt = tokens.get("refresh_token", _token_cache["refresh_token"])
    _token_cache["access_token"]  = tokens["access_token"]
    _token_cache["refresh_token"] = new_rt
    _token_cache["expires_at"]    = datetime.now().timestamp() + tokens.get("expires_in", 3600) - 120

    await _save_refresh_token_to_render(new_rt)
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

def _extrair_items(data: object) -> tuple[list, int, object]:
    """
    Extrai a lista de itens de uma resposta da API, independente do formato.
    Retorna (items, total, raw_data_para_debug).

    Tenta, em ordem:
      1. dict com chave "items", "content", "data" ou "lancamentos"
      2. lista diretamente (API retornou array)
      3. lista vazia + raw para debug
    """
    if isinstance(data, list):
        return data, len(data), None

    if isinstance(data, dict):
        for chave in ("itens", "items", "content", "data", "lancamentos", "eventos", "registros"):
            if data.get(chave) is not None:
                items = data[chave] if isinstance(data[chave], list) else []
                total = (
                    data.get("itens_totais")  # Conta Azul API v2
                    or data.get("total")
                    or data.get("totalElements")
                    or data.get("total_registros")
                    or len(items)
                )
                return items, int(total), None

    # Nenhum formato reconhecido — devolve raw para debug
    return [], 0, data

async def _get_all(path: str, params: dict) -> tuple[list, object]:
    """
    Itera todas as páginas de um endpoint paginado e retorna (lista_completa, debug_info).
    debug_info é None quando tudo correu bem; caso contrário, contém a resposta bruta
    da primeira página para diagnóstico.
    """
    items = []
    pagina = 1
    debug_raw = None

    while True:
        p = {**params, "pagina": pagina, "tamanho_pagina": 200}
        data = await _get(path, p)
        page_items, total, raw = _extrair_items(data)

        if pagina == 1 and raw is not None:
            # Formato não reconhecido — salva raw e para
            debug_raw = raw
            break

        items.extend(page_items)

        if len(items) >= total or not page_items:
            break
        pagina += 1

    return items, debug_raw

_STATUS_MAP = {
    "ACQUITTED": "QUITADO",
    "PENDING":   "PENDENTE",
    "OVERDUE":   "ATRASADO",
    "CANCELLED": "CANCELADO",
    "QUITADO":    "QUITADO",
    "CONCILIADO": "CONCILIADO",
    "PENDENTE":   "PENDENTE",
    "ATRASADO":   "ATRASADO",
    "CANCELADO":  "CANCELADO",
}

def _normalizar(items: list, tipo: str) -> list:
    """
    Normaliza itens da API Conta Azul v2 para formato flat.
    API v2: total/pago, categorias[], centros_de_custo[], fornecedor/cliente, status em ingles.
    """
    result = []
    for item in items:
        parcelas = item.get("parcelas") or [item]
        for p in parcelas:
            pessoa = (
                p.get("fornecedor") or p.get("cliente")
                or item.get("fornecedor") or item.get("cliente")
                or item.get("pessoa") or {}
            )
            valor      = p.get("total") if p.get("total") is not None else p.get("valor")
            valor_pago = p.get("pago")  if p.get("pago")  is not None else p.get("valor_pago")
            cats = p.get("categorias") or ([p["categoria"]] if p.get("categoria") else [])
            categoria = cats[0].get("nome") if cats else None
            ccs = p.get("centros_de_custo") or ([p["centro_de_custo"]] if p.get("centro_de_custo") else [])
            centro_custo = ccs[0].get("nome") if ccs else None
            status_raw = p.get("status") or item.get("status")
            status = _STATUS_MAP.get(status_raw, status_raw)
            result.append({
                "tipo":         tipo,
                "fornecedor":   pessoa.get("nome") if isinstance(pessoa, dict) else None,
                "descricao":    p.get("descricao") or item.get("descricao"),
                "categoria":    categoria,
                "centro_custo": centro_custo,
                "valor":        valor,
                "valor_pago":   valor_pago,
                "status":       status,
                "vencimento":   p.get("data_vencimento"),
                "pagamento":    p.get("data_pagamento"),
            })
    return result


# ── MCP Server ────────────────────────────────────────────────────────────────
server = Server("conta-azul")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="buscar_lancamentos",
            description=(
                "Busca lançamentos financeiros (contas a pagar ou receber) por período. "
                "Usa data_vencimento por padrão — inclui Em aberto e Atrasado. "
                "Retorna lista flat com fornecedor, valor, categoria, centro de custo e status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_inicio": {"type": "string", "description": "Data inicial YYYY-MM-DD"},
                    "data_fim":    {"type": "string", "description": "Data final YYYY-MM-DD"},
                    "tipo":        {"type": "string", "enum": ["DESPESA", "RECEITA"], "default": "DESPESA"},
                    "status":      {"type": "string", "enum": ["PENDENTE", "QUITADO", "ATRASADO", "CANCELADO"], "description": "Opcional — filtra por status"},
                    "filtro_data": {"type": "string", "enum": ["vencimento", "pagamento"], "default": "vencimento", "description": "Qual data usar como filtro. Padrão: vencimento (inclui Em aberto). Use pagamento para pegar só os já quitados."},
                },
                "required": ["data_inicio", "data_fim"],
            },
        ),
        Tool(
            name="buscar_lancamentos_por_cc",
            description=(
                "Busca despesas do período agrupadas por centro de custo. "
                "Retorna por CC: total e lista de lançamentos com fornecedor, valor, status e vencimento. "
                "Usa data_vencimento para incluir Em aberto e Atrasado. "
                "Use para análise de despesas por área (COGS, S&M, R&D, G&A)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_inicio": {"type": "string", "description": "YYYY-MM-DD"},
                    "data_fim":    {"type": "string", "description": "YYYY-MM-DD"},
                    "status":      {"type": "string", "description": "Opcional: QUITADO, PENDENTE, ATRASADO"},
                },
                "required": ["data_inicio", "data_fim"],
            },
        ),
        Tool(
            name="diff_semanal",
            description=(
                "Compara lançamentos de dois períodos por centro de custo. "
                "Identifica: novos (➕), liquidados — passaram de Em aberto/Atrasado para Quitado/Conciliado (✅), "
                "e alterados — valor mudou (✏️). "
                "Retorna diff estruturado por CC pronto para análise narrativa do Processo ①. "
                "Use data_inicio_atual = primeiro dia do mês para comparar acumulados."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_inicio_atual":    {"type": "string", "description": "Início do período atual YYYY-MM-DD"},
                    "data_fim_atual":       {"type": "string", "description": "Fim do período atual YYYY-MM-DD (hoje)"},
                    "data_inicio_anterior": {"type": "string", "description": "Início do período anterior YYYY-MM-DD (igual ao atual)"},
                    "data_fim_anterior":    {"type": "string", "description": "Fim do período anterior YYYY-MM-DD (terça passada)"},
                },
                "required": ["data_inicio_atual", "data_fim_atual", "data_inicio_anterior", "data_fim_anterior"],
            },
        ),
        Tool(
            name="extrato_mensal",
            description=(
                "Retorna o extrato completo do mês — receitas e despesas — "
                "com fornecedor/cliente, valor, categoria, centro de custo e status. "
                "Inclui todos os status (Em aberto, Atrasado, Quitado). "
                "Se não informar mês/ano, usa o mês atual."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mes":    {"type": "integer", "description": "Mês (1-12). Padrão: mês atual."},
                    "ano":    {"type": "integer", "description": "Ano (ex: 2026). Padrão: ano atual."},
                    "status": {"type": "string", "description": "Opcional: PENDENTE, QUITADO, ATRASADO, CANCELADO"},
                },
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

    # ── buscar_lancamentos ────────────────────────────────────────────────────
    if name == "buscar_lancamentos":
        tipo     = args.get("tipo", "DESPESA")
        filtro   = args.get("filtro_data", "vencimento")
        endpoint = "contas-a-pagar" if tipo == "DESPESA" else "contas-a-receber"
        params   = {
            f"data_{filtro}_de":  args["data_inicio"],
            f"data_{filtro}_ate": args["data_fim"],
        }
        if args.get("status"):
            params["status"] = args["status"]
        items, debug = await _get_all(f"/financeiro/eventos-financeiros/{endpoint}/buscar", params)
        result = _normalizar(items, tipo)
        if debug is not None:
            return {"_erro_formato_api": debug, "lancamentos": result}
        return result

    # ── buscar_lancamentos_por_cc ─────────────────────────────────────────────
    elif name == "buscar_lancamentos_por_cc":
        params = {
            "data_vencimento_de":  args["data_inicio"],
            "data_vencimento_ate": args["data_fim"],
        }
        if args.get("status"):
            params["status"] = args["status"]
        items, debug = await _get_all("/financeiro/eventos-financeiros/contas-a-pagar/buscar", params)
        if debug is not None:
            return {"_erro_formato_api": debug}
        lancamentos = _normalizar(items, "DESPESA")

        por_cc: dict[str, list] = {}
        for l in lancamentos:
            cc = l["centro_custo"] or "Sem CC"
            por_cc.setdefault(cc, []).append(l)

        return {
            cc: {
                "total": round(sum(l["valor"] or 0 for l in lista), 2),
                "lancamentos": sorted(lista, key=lambda x: x["vencimento"] or ""),
            }
            for cc, lista in sorted(por_cc.items())
        }

    # ── diff_semanal ──────────────────────────────────────────────────────────
    elif name == "diff_semanal":
        endpoint = "/financeiro/eventos-financeiros/contas-a-pagar/buscar"
        LIQUIDADOS = {"QUITADO", "CONCILIADO"}

        items_atual, debug1 = await _get_all(endpoint, {
            "data_vencimento_de":  args["data_inicio_atual"],
            "data_vencimento_ate": args["data_fim_atual"],
        })
        items_anterior, debug2 = await _get_all(endpoint, {
            "data_vencimento_de":  args["data_inicio_anterior"],
            "data_vencimento_ate": args["data_fim_anterior"],
        })

        if debug1 or debug2:
            return {"_erro_formato_api": {"atual": debug1, "anterior": debug2}}

        atual    = _normalizar(items_atual,    "DESPESA")
        anterior = _normalizar(items_anterior, "DESPESA")

        def _chave(l: dict) -> str:
            return f"{l['fornecedor']}|{l['descricao']}|{l['vencimento']}"

        map_anterior = {_chave(l): l for l in anterior}
        map_atual    = {_chave(l): l for l in atual}

        diff: dict[str, dict] = {}

        for chave, l in map_atual.items():
            cc = l["centro_custo"] or "Sem CC"
            if cc not in diff:
                diff[cc] = {"novos": [], "liquidados": [], "alterados": [], "sem_mudanca": []}

            if chave not in map_anterior:
                diff[cc]["novos"].append(l)                                         # ➕ Novo
            elif l["status"] in LIQUIDADOS and map_anterior[chave]["status"] not in LIQUIDADOS:
                diff[cc]["liquidados"].append(l)                                    # ✅ Liquidado
            elif l["valor"] != map_anterior[chave]["valor"]:
                diff[cc]["alterados"].append({                                      # ✏️ Alterado
                    **l,
                    "valor_anterior": map_anterior[chave]["valor"],
                })
            else:
                diff[cc]["sem_mudanca"].append(l)

        resumo = {}
        for cc, grupos in sorted(diff.items()):
            tem_mudanca = grupos["novos"] or grupos["liquidados"] or grupos["alterados"]
            resumo[cc] = {
                "mudancas": tem_mudanca,
                "novos":      grupos["novos"],
                "liquidados": grupos["liquidados"],
                "alterados":  grupos["alterados"],
                "sem_mudanca_count": len(grupos["sem_mudanca"]),
            }
        return resumo

    # ── extrato_mensal ────────────────────────────────────────────────────────
    elif name == "extrato_mensal":
        hoje        = datetime.now()
        mes         = args.get("mes") or hoje.month
        ano         = args.get("ano") or hoje.year
        ultimo_dia  = calendar.monthrange(ano, mes)[1]
        data_inicio = f"{ano}-{mes:02d}-01"
        data_fim    = f"{ano}-{mes:02d}-{ultimo_dia}"

        params_base = {
            "data_vencimento_de":  data_inicio,
            "data_vencimento_ate": data_fim,
        }
        if args.get("status"):
            params_base["status"] = args["status"]

        items_d, debug_d = await _get_all("/financeiro/eventos-financeiros/contas-a-pagar/buscar",  params_base)
        items_r, debug_r = await _get_all("/financeiro/eventos-financeiros/contas-a-receber/buscar", params_base)

        # Se a API devolveu formato não reconhecido, expõe o raw para diagnóstico
        if debug_d is not None or debug_r is not None:
            return {
                "periodo": f"{mes:02d}/{ano}",
                "_erro_formato_api": {
                    "despesas_raw": debug_d,
                    "receitas_raw": debug_r,
                },
                "resumo": {"total_receitas": 0, "total_despesas": 0, "saldo": 0, "qtd_receitas": 0, "qtd_despesas": 0},
                "despesas": [],
                "receitas": [],
            }

        despesas = _normalizar(items_d, "DESPESA")
        receitas = _normalizar(items_r, "RECEITA")

        total_receitas = round(sum(i["valor"] or 0 for i in receitas), 2)
        total_despesas = round(sum(i["valor"] or 0 for i in despesas), 2)

        return {
            "periodo": f"{mes:02d}/{ano}",
            "resumo": {
                "total_receitas": total_receitas,
                "total_despesas": total_despesas,
                "saldo":          round(total_receitas - total_despesas, 2),
                "qtd_receitas":   len(receitas),
                "qtd_despesas":   len(despesas),
            },
            "despesas": despesas,
            "receitas": receitas,
        }

    # ── saldo_contas ──────────────────────────────────────────────────────────
    elif name == "saldo_contas":
        contas = await _get("/conta-financeira", {"pagina": 1, "tamanho_pagina": 20, "apenas_ativo": "true"})
        items, _ = _extrair_items(contas)
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

    # ── listar_categorias ─────────────────────────────────────────────────────
    elif name == "listar_categorias":
        params: dict = {"pagina": 1, "tamanho_pagina": 100}
        if args.get("tipo"):
            params["tipo"] = args["tipo"]
        return await _get("/categorias", params)

    # ── listar_centros_de_custo ───────────────────────────────────────────────
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

    rt = tokens["refresh_token"]
    _token_cache["access_token"]  = tokens["access_token"]
    _token_cache["refresh_token"] = rt
    _token_cache["expires_at"]    = datetime.now().timestamp() + tokens.get("expires_in", 3600) - 120

    await _save_refresh_token_to_render(rt)

    return HTMLResponse("""
    <h1>✅ Autorizado com sucesso!</h1>
    <p>Token salvo automaticamente. Pode fechar essa janela — o MCP está pronto.</p>
    """)

async def health(request: Request):
    return HTMLResponse("ok")

# ── ASGI app ──────────────────────────────────────────────────────────────────
sse = SseServerTransport("/messages")

async def handle_sse(request: Request):
async with sse.connect_sse(request.scope, request.receive, request.scope["send"]) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())

app = Starlette(routes=[
    Route("/auth",     auth_redirect),
    Route("/callback", oauth_callback),
    Route("/health",   health),
    Route("/sse",      handle_sse),
    Mount("/messages", app=sse.handle_post_message),
])
