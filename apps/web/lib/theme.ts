// Shared with the server layout: do not put this module behind a 'use client' boundary.
export const THEME_STORAGE_KEY = 'paperforge-theme';

/** 注入到 <head> 的同步脚本：在 hydration 前打好 class，避免深色下先闪一屏白。 */
export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem('${THEME_STORAGE_KEY}');var d=t==='dark'||(!t&&window.matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.classList.toggle('dark',d)}catch(e){}})()`;
