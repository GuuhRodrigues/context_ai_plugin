# Passo a passo: como testar a extensão Context AI Plugin

Siga estes passos na ordem para testar a extensão no Chrome ou Edge.

---

## Parte 1: Preparar o backend

### Passo 1.1 — Abrir o terminal na pasta do projeto

- Abra o terminal (PowerShell, CMD ou Git Bash).
- Vá para a pasta do repositório:
  ```bash
  cd c:\Users\Guuh\repositories\context_ai_plugin
  ```

### Passo 1.2 — Garantir que o `.env` tem a chave da OpenAI

- Abra o arquivo `.env` na raiz do projeto.
- Deve existir uma linha assim (com sua chave real):
  ```
  OPENAI_API_KEY=sk-...
  ```
- Se não tiver, crie a linha e salve o arquivo.

### Passo 1.3 — Subir a API e os workers

No terminal:

```bash
docker compose up --build
```

- Espere aparecer algo como: `Uvicorn running on http://0.0.0.0:8000`.
- Deixe esse terminal aberto (os serviços precisam ficar rodando).

### Passo 1.4 — Conferir se a API está no ar

Em **outro** terminal:

```bash
curl http://localhost:8000/health
```

- Resposta esperada: `{"status":"ok"}`.
- Se der erro de conexão, volte ao Passo 1.3 e confira se o Docker subiu sem erros.

---

## Parte 2: Instalar a extensão no navegador

### Passo 2.1 — Abrir a página de extensões

- **Chrome:** na barra de endereço digite: `chrome://extensions/`
- **Edge:** digite: `edge://extensions/`
- Pressione Enter.

### Passo 2.2 — Ativar o modo desenvolvedor

- No canto superior direito, encontre o switch **“Modo do desenvolvedor”** (ou “Developer mode”).
- Ative-o (deve ficar azul/ligado).

### Passo 2.3 — Carregar a extensão

- Clique no botão **“Carregar sem compactação”** (ou “Load unpacked”).
- Na janela que abrir, navegue até a pasta da extensão:
  ```
  c:\Users\Guuh\repositories\context_ai_plugin\extension
  ```
- Selecione a pasta **`extension`** (e não um arquivo dentro dela).
- Clique em **“Selecionar pasta”** (ou “Select Folder”).

### Passo 2.4 — Verificar se carregou

- Na lista de extensões deve aparecer **“Context AI Plugin”**.
- Se aparecer um aviso de ícones faltando, pode ignorar por enquanto (a extensão funciona; o ícone na barra será o padrão do Chrome/Edge).
- Anote onde está o ícone da extensão (barra de ferramentas ou menu de extensões) para o próximo passo.

---

## Parte 3: Testar a extensão

### Passo 3.1 — Abrir o painel da extensão

- Clique no ícone da extensão **Context AI Plugin** na barra de ferramentas (ou no menu de extensões).
- Deve abrir o **Side Panel** com:
  - Título “Context AI”
  - Botão **“Iniciar Reunião”**
  - Status “Desconectado”.

### Passo 3.2 — Iniciar uma reunião

- Clique em **“Iniciar Reunião”**.
- O navegador deve solicitar **permissão de microfone**:
  - Clique em **“Permitir”**.
- A extensão tenta capturar o **áudio da aba ativa** (ex.: Google Meet/Teams).
- Em navegadores sem `tabCapture`, será aberto o seletor de compartilhamento:
  - Escolha a aba da reunião.
  - Marque **“Compartilhar áudio”**.
- Em alguns segundos:
  - O status deve mudar para **“Conectado”**.
  - Aparecem **“Reunião”** e **“Token expira em”**.
  - O botão **“Iniciar Reunião”** some e aparecem **“Encerrar Reunião”** e **“Renovar Token”**.

Se aparecer erro (ex.: “Erro ao iniciar reunião”):
- Confira se o backend está rodando (Passo 1.3 e 1.4).
- Verifique se você permitiu o microfone.
- Verifique se o Side Panel está aberto com a **mesma aba da reunião** ativa.
- Recarregue a extensão em `chrome://extensions/` e tente de novo.

### Passo 3.3 — Testar transcrição

- Com a reunião ativa, mantenha a aba da reunião emitindo áudio e fale normalmente.
- Em alguns segundos, na seção **“Transcrição”**, devem aparecer linhas com o que foi dito.
- Se não aparecer:
  - Verifique se a aba da reunião está com áudio ativo.
  - Fale de novo e espere até ~30 segundos (o worker pode demorar um pouco para processar).

### Passo 3.4 — Ver insights (se aparecerem)

- Conforme você fala, o sistema pode gerar **insights** (resumos, pontos-chave, termos).
- Eles aparecem na seção **“Insights”**.
- Pode levar 1–2 minutos e depende do conteúdo da fala; se não aparecer nada, não é necessariamente erro.

### Passo 3.5 — Ver Q&A (se aparecer)

- Se você fizer uma pergunta direta (ex.: “O que é machine learning?”), pode aparecer um bloco na seção **“Perguntas & Respostas”** com pergunta e resposta.
- Também pode demorar um pouco; não é obrigatório aparecer em todo teste.

### Passo 3.6 — Renovar token (opcional)

- Com a reunião ativa, clique em **“Renovar Token”**.
- Deve aparecer uma mensagem de sucesso (ex.: “Token renovado com sucesso!”) e o tempo de expiração deve ser atualizado.
- Isso simula o uso em reuniões longas.

### Passo 3.7 — Encerrar a reunião

- Clique em **“Encerrar Reunião”**.
- O status volta para **“Desconectado”**.
- As seções de Transcrição, Insights e Q&A podem ser limpas ou ficar como estavam (comportamento pode variar).
- O botão **“Iniciar Reunião”** volta a aparecer.

---

## Resumo rápido (checklist)

- [ ] Terminal na pasta do projeto
- [ ] `.env` com `OPENAI_API_KEY`
- [ ] `docker compose up --build` rodando
- [ ] `curl http://localhost:8000/health` retorna `{"status":"ok"}`
- [ ] Modo desenvolvedor ativado em `chrome://extensions/` ou `edge://extensions/`
- [ ] Extensão carregada a partir da pasta `extension`
- [ ] Popup aberta na aba da reunião → “Iniciar Reunião”
- [ ] Status “Conectado” e transcrição aparecendo ao falar
- [ ] (Opcional) “Renovar Token” e “Encerrar Reunião” testados

---

## Problemas comuns

| Problema | O que fazer |
|---------|-------------|
| “Erro ao iniciar reunião” | Backend rodando? `curl http://localhost:8000/health` responde? |
| Nada na transcrição | Microfone permitido e aba com áudio ativo? Falar de novo e esperar ~30 s. |
| Extensão não carrega (ícones) | Remova as entradas `default_icon` e `icons` do `manifest.json` temporariamente e recarregue a extensão. |
| Popup não abre | Recarregue a extensão em `chrome://extensions/` (botão de atualizar) e tente de novo. |
| CORS ou rede | Por padrão a extensão usa `http://localhost:8000`; confira se a API está nessa porta. |

Se quiser, na próxima etapa podemos ajustar o `manifest.json` para não depender dos ícones e você só recarregar a extensão.
