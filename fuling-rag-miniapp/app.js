import { BASE_URL } from './utils/config';

App({
  globalData: {
    // Single source of truth for the API host lives in utils/config.js.
    baseUrl: BASE_URL,
    token: '',
    userId: '',
    displayName: '',
    dept: '',
    corpId: '',
  },

  onLaunch(options) {
    // options.query may contain corpId when launched from a DingTalk workbench entry.
    if (options && options.query && options.query.corpId) {
      this.globalData.corpId = options.query.corpId;
    }
    // Login itself is performed lazily on the chat page (pages/chat/chat.js -> onLoad),
    // so a cold launch into the settings tab does not block on a network round-trip.
  },

  onError(err) {
    // Centralised crash logging hook. Replace with your APM/monitor as needed.
    console.error('[app.onError]', err);
  },
});
