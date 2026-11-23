document.querySelectorAll('.notice[data-auto-dismiss]').forEach(notice => {
  window.setTimeout(() => {
    notice.classList.add('is-dismissing');
    window.setTimeout(() => notice.remove(), 200);
  }, 4800);
});
