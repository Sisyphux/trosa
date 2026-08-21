chrome.runtime.onInstalled.addListener(() => {
  if (chrome.sidePanel?.setPanelBehavior) chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

chrome.action.onClicked.addListener(async (tab) => {
  if (chrome.sidePanel?.open && tab?.windowId) {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  } else {
    // Older Chromium builds get the same usable UI in a normal extension tab.
    await chrome.tabs.create({ url: chrome.runtime.getURL('sidepanel.html') });
  }
});
