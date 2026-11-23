document.querySelectorAll(".notice.success, .notice.error").forEach((notice) => {
  const isError = notice.classList.contains("error");
  const dismiss = () => {
    if (notice.classList.contains("is-dismissing")) return;
    notice.style.maxHeight = `${notice.scrollHeight}px`;
    requestAnimationFrame(() => notice.classList.add("is-dismissing"));
    window.setTimeout(() => notice.remove(), 350);
  };

  notice.classList.add("is-dismissible");
  notice.setAttribute("role", isError ? "alert" : "status");
  notice.setAttribute("aria-live", isError ? "assertive" : "polite");

  const closeButton = document.createElement("button");
  closeButton.className = "notice-dismiss";
  closeButton.type = "button";
  closeButton.setAttribute("aria-label", "Dismiss notification");
  closeButton.innerHTML = "&times;";
  closeButton.addEventListener("click", dismiss);
  notice.append(closeButton);

  window.setTimeout(dismiss, isError ? 5000 : 3000);
});
