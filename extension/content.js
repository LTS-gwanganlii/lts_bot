function getVideo() {
  return document.querySelector('video');
}

async function waitVideo(timeoutMs = 4000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const v = getVideo();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 100));
  }
  return null;
}

async function runAction(message) {
  const action = message.action;

  if (action === 'search_and_play') {
    const q = encodeURIComponent(message.query || '');
    const url = `https://www.youtube.com/results?search_query=${q}`;
    if (!location.href.startsWith(url)) {
      location.href = url;
      return;
    }

    const first = document.querySelector('ytd-video-renderer a#thumbnail');
    if (first) {
      first.click();
    }
    return;
  }

  const video = await waitVideo();
  if (!video) return;

  if (action === 'play') {
    await video.play().catch(() => {});
  } else if (action === 'pause') {
    video.pause();
  } else if (action === 'seek') {
    const sec = Number(message.seconds || 0);
    video.currentTime = Math.max(0, video.currentTime + sec);
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  runAction(message)
    .then(() => sendResponse({ ok: true }))
    .catch((err) => sendResponse({ ok: false, error: String(err) }));
  return true;
});
