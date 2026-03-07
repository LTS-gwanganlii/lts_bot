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
    const url = `https://www.youtube.com/results?search_query=${q}&lts_play=1`;
    if (!location.href.includes(`search_query=${q}`)) {
      location.href = url;
      return;
    }

    // Try finding the first video immediately if already on search page
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

// Auto-play the first item if lts_play=1 is present in the URL
function checkAutoPlay() {
  if (location.href.includes('lts_play=1')) {
    const checkInterval = setInterval(() => {
      const first = document.querySelector('ytd-video-renderer a#thumbnail');
      if (first && first.href) {
        clearInterval(checkInterval);
        
        // Remove the flag so it doesn't trigger again on back button
        const newUrl = location.href.replace('&lts_play=1', '').replace('?lts_play=1', '');
        history.replaceState(null, '', newUrl);
        
        first.click();
      }
    }, 500);

    // Stop checking after 10 seconds just in case
    setTimeout(() => clearInterval(checkInterval), 10000);
  }
}

window.addEventListener('load', checkAutoPlay);
// In case of SPA transitions without full reload
window.addEventListener('yt-navigate-finish', checkAutoPlay);
