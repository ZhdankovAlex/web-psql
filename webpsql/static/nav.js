// The collapsed side panel is remembered in localStorage.
// Loaded in <head> so the class is set before the first paint and the panel does not jump.
(() => {
  try { if (localStorage.getItem("nav-collapsed") === "1") document.documentElement.classList.add("nav-collapsed"); } catch (e) {}
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("nav-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const collapsed = document.documentElement.classList.toggle("nav-collapsed");
      try { localStorage.setItem("nav-collapsed", collapsed ? "1" : "0"); } catch (e) {}
      window.dispatchEvent(new Event("resize")); // the terminal recalculates its size
    });
  });
})();
