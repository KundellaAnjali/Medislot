document.documentElement.classList.add("ui-enhanced");
requestAnimationFrame(() => document.body.classList.add("is-ready"));

const revealTargets = document.querySelectorAll(
  ".patient-profile, .doctor-search, .patient-availability, .public-doctor-section, " +
  ".doctor-search-card, .availability-slot-card, .metric-panel, .dashboard-panel, " +
  ".patient-home-hero, .patient-home-actions, .patient-home-card"
);

if ("IntersectionObserver" in window) {
  const revealObserver = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          revealObserver.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.08 }
  );
  revealTargets.forEach((target, index) => {
    target.style.setProperty("--reveal-delay", `${Math.min(index * 35, 210)}ms`);
    revealObserver.observe(target);
  });
} else {
  revealTargets.forEach(target => target.classList.add("is-visible"));
}

document.querySelectorAll("form").forEach(form => {
  form.addEventListener("submit", () => {
    if (!form.checkValidity()) return;
    const submitButton = form.querySelector(
      'button[type="submit"]:not([disabled]), input[type="submit"]:not([disabled])'
    );
    if (!submitButton) return;
    submitButton.disabled = true;
    submitButton.classList.add("is-loading");
  });
});

document.querySelectorAll("button, .button").forEach(control => {
  control.addEventListener("pointerdown", event => {
    const rect = control.getBoundingClientRect();
    control.style.setProperty("--ripple-x", `${event.clientX - rect.left}px`);
    control.style.setProperty("--ripple-y", `${event.clientY - rect.top}px`);
  });
});
