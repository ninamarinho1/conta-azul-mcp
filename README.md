# Conta Azul MCP Server

Servidor MCP para integrar o Conta Azul ao Claude.ai.

## Deploy no Railway

### 1. Criar projeto no Railway
- Acesse https://railway.app e faça login com GitHub
- New Project → Deploy from GitHub repo → selecione este repositório

### 2. Configurar variáveis de ambiente
No painel do Railway → Settings → Variables, adicione:

| Variável | Valor |
|---|---|
| `CONTA_AZUL_CLIENT_ID` | (do portal de devs do Conta Azul) |
| `CONTA_AZUL_CLIENT_SECRET` | (do portal de devs do Conta Azul) |
| `CONTA_AZUL_REDIRECT_URI` | `https://SEU_APP.up.railway.app/callback` |

### 3. Obter a URL do app
Após deploy, Railway gera uma URL tipo `https://conta-azul-mcp-production.up.railway.app`

### 4. Atualizar redirect URI no Conta Azul
- No portal de devs do Conta Azul, edite o app e atualize a URL de redirecionamento
- Use: `https://SUA_URL_RAILWAY/callback`
- Atualize também a variável `CONTA_AZUL_REDIRECT_URI` no Railway

### 5. Autorizar acesso (uma vez)
- Abra no navegador: `https://SUA_URL_RAILWAY/auth`
- Faça login no Conta Azul
- Copie o `refresh_token` exibido na tela
- Adicione como variável `CONTA_AZUL_REFRESH_TOKEN` no Railway

### 6. Conectar no Claude.ai
- Settings → Integrations → Add MCP Server
- URL: `https://SUA_URL_RAILWAY/sse`

## Tools disponíveis

- **buscar_lancamentos** — contas a pagar/receber por período e status
- **buscar_lancamentos_por_categoria** — despesas agrupadas por categoria
- **saldo_contas** — saldo atual de todas as contas
- **listar_categorias** — categorias financeiras cadastradas
- **listar_centros_de_custo** — centros de custo ativos
