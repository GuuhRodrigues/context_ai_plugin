# Context AI Plugin - Browser Extension

Extensão de navegador (Chrome/Edge) para o Context AI Plugin - Meeting Copilot em tempo real.

## 📦 Instalação

### Desenvolvimento (modo não empacotado)

1. Abra o Chrome/Edge
2. Vá para `chrome://extensions/` (ou `edge://extensions/`)
3. Ative o "Modo do desenvolvedor" (toggle no canto superior direito)
4. Clique em "Carregar sem compactação"
5. Selecione a pasta `extension/` deste repositório

### Configuração

Por padrão, a extensão se conecta em `http://localhost:8000`. Para mudar:

1. Abra a extensão (clique no ícone)
2. O `API_BASE_URL` é armazenado no `chrome.storage.local`
3. Você pode modificá-lo editando `background.js` ou adicionando uma UI de configuração

## 🚀 Uso

1. **Abrir painel da extensão**: clique no ícone da extensão para abrir o Side Panel.

2. **Iniciar Reunião**: Clique em "Iniciar Reunião"
   - A extensão cria uma nova reunião na API
   - Solicita acesso ao microfone
   - Captura o áudio da aba ativa (ex.: Google Meet/Teams)
   - Conecta nos WebSockets (áudio + eventos)

3. **Ver Transcrição**: A transcrição aparece em tempo real na seção "Transcrição"

4. **Ver Insights**: Insights gerados aparecem na seção "Insights"

5. **Ver Q&A**: Perguntas detectadas e respostas aparecem na seção "Perguntas & Respostas"

6. **Renovar Token**: Clique em "Renovar Token" antes do token expirar (ou aguarde auto-refresh quando <1h)

7. **Encerrar Reunião**: Clique em "Encerrar Reunião" para fechar e limpar tudo

## 🔧 Estrutura

- `manifest.json`: Configuração da extensão (Manifest V3)
- `background.js`: Service worker que gerencia reuniões e API calls
- `popup.html`: Interface principal (abre ao clicar no ícone)
- `popup.js`: Lógica da UI e conexões WebSocket
- `content.js`: Script injetado nas páginas (placeholder para futuro overlay)
- `styles.css`: Estilos da popup

## 📝 Notas

- A extensão usa `tabCapture` para capturar áudio da aba ativa
- Quando `tabCapture` não estiver disponível, tenta `getDisplayMedia` e depois `desktopCapture`
- A extensão também solicita permissão de microfone no início da reunião
- WebSockets reconectam automaticamente em caso de desconexão
- Token é renovado automaticamente quando falta <1h
- Dados da reunião são armazenados localmente (`chrome.storage.local`)

## 🎨 Ícones

Você precisa criar os ícones em `icons/`:
- `icon16.png` (16x16)
- `icon48.png` (48x48)
- `icon128.png` (128x128)

Ou use um gerador online como https://www.favicon-generator.org/
