# Como testar a extensao Context AI Plugin

## Parte 1: Backend

1. Ir para a pasta do projeto:
```bash
cd c:\Users\Guuh\repositories\context_ai_plugin
```

2. Confirmar `.env` com:
```env
OPENAI_API_KEY=sk-...
```

3. Subir servicos:
```bash
docker compose up --build
```

4. Validar API:
```bash
curl http://localhost:8000/health
```
Esperado: `{"status":"ok"}`.

## Parte 2: Extensao

1. Abrir `chrome://extensions/` (ou `edge://extensions/`).
2. Ativar Developer Mode.
3. Clicar em `Load unpacked`.
4. Selecionar `c:\Users\Guuh\repositories\context_ai_plugin\extension`.

## Parte 3: Fluxo funcional

1. Abrir popup da extensao.
2. Clicar em `Iniciar Reuniao`.
3. Permitir microfone e compartilhar audio da aba quando solicitado.
4. Confirmar status `Conectado`.
5. Falar e verificar transcricao na popup.
6. Testar `Renovar Token` (opcional).
7. Testar `Encerrar Reuniao`.

## Parte 4: Validacao de VAD (novo)

Objetivo: reduzir envio de silencio para baixar custo.

1. Iniciar reuniao normalmente.
2. Abrir console da popup.
3. Falar por 5-10s.
4. Ficar em silencio por ~10s.
5. Confirmar que logs `[VAD] sending audio ...` aparecem durante fala e caem bastante no silencio.
6. Voltar a falar e confirmar retomada imediata dos logs.
7. No `report_*.md`, conferir `VAD gating active: yes`, `VAD bypass reason: none` e `VAD estimated savings` maior que zero em trechos de silencio.

## Parte 5: Validacao de observabilidade

1. Encerrar a reuniao pelo botao `Encerrar Reuniao`.
2. Conferir se foi criada uma pasta em:
```text
observability/meetings/{meeting_id}/
```
3. Confirmar que existem tres arquivos com timestamp:
```text
events_YYYY_MM_DD_HH_MM_SS.jsonl
metrics_YYYY_MM_DD_HH_MM_SS.json
report_YYYY_MM_DD_HH_MM_SS.md
```
4. Abrir `report_*.md` e validar se aparecem transcricao, custo estimado, tokens, VAD, decisoes e latencias por tipo.

## Checklist

- [ ] API responde em `/health`
- [ ] Reuniao inicia e conecta WebSockets
- [ ] Transcricao aparece com audio da aba
- [ ] Token pode ser renovado
- [ ] Reuniao fecha corretamente
- [ ] VAD reduz envio em silencio
- [ ] Arquivos de observabilidade sao gerados apos encerrar
