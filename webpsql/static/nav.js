// Свёрнутая боковая панель запоминается в localStorage.
// Скрипт подключается в <head>, чтобы класс появился до первой отрисовки и панель не прыгала.
(() => {
  try { if (localStorage.getItem("nav-collapsed") === "1") document.documentElement.classList.add("nav-collapsed"); } catch (e) {}
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("nav-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const collapsed = document.documentElement.classList.toggle("nav-collapsed");
      try { localStorage.setItem("nav-collapsed", collapsed ? "1" : "0"); } catch (e) {}
      window.dispatchEvent(new Event("resize")); // терминал пересчитает размер
    });
  });
})();
