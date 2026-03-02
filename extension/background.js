const WS_URL = 'ws://localhost:8765';
let ws = null;
let reconnectTimer = null;
let mutex = Promise.resolve();

function withMutex(task) {
  mutex = mutex.then(task).catch((err) => {
    console.error('mutex error', err);
  });
  return mutex;
}

function connectWebSocket() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log('Connected to python websocket server');
  };

  ws.onmessage = (event) => {
    try {
      const cmd = JSON.parse(event.data);
      withMutex(() => handleCommand(cmd));
    } catch (e) {
      console.error('Invalid command payload', e);
    }
  };

  ws.onclose = () => {
    scheduleReconnect();
  };

  ws.onerror = () => {
    ws.close();
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWebSocket();
  }, 1500);
}

async function getYoutubeTabs() {
  return chrome.tabs.query({ url: '*://*.youtube.com/*' });
}

async function sendMessageSafe(tabId, payload) {
  try {
    await chrome.tabs.sendMessage(tabId, payload);
  } catch (e) {
    console.warn('content message failed', tabId, e);
  }
}

async function handleCommand(cmd) {
  const tabs = await getYoutubeTabs();
  if (!tabs.length) return;

  const action = cmd.action;

  if (action === 'search_and_play') {
    await Promise.all(tabs.map((t) => sendMessageSafe(t.id, { action: 'pause' })));
    await sendMessageSafe(tabs[0].id, { action: 'search_and_play', query: cmd.query || '' });
    return;
  }

  if (action === 'pause' || action === 'play') {
    await Promise.all(tabs.map((t) => sendMessageSafe(t.id, { action })));
    return;
  }

  if (action === 'seek') {
    await sendMessageSafe(tabs[0].id, { action: 'seek', seconds: Number(cmd.seconds || 0) });
  }
}

connectWebSocket();
