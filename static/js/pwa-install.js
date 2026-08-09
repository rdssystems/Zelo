(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
    });
  }

  var isStandalone =
    window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  var deferredPrompt = null;

  function buttons() {
    return document.querySelectorAll("[data-pwa-install-button]");
  }
  function showButtons() {
    buttons().forEach(function (btn) {
      btn.classList.remove("hidden");
    });
  }
  function hideButtons() {
    buttons().forEach(function (btn) {
      btn.classList.add("hidden");
    });
  }

  if (!isStandalone) {
    if (isIOS) {
      // Safari iOS nunca dispara beforeinstallprompt — o botão fica visível
      // desde o início e explica o passo manual (Compartilhar > Adicionar à
      // Tela de Início) em vez de esperar um evento que nunca chega.
      document.addEventListener("DOMContentLoaded", showButtons);
    } else {
      window.addEventListener("beforeinstallprompt", function (event) {
        event.preventDefault();
        deferredPrompt = event;
        showButtons();
      });
    }
  }

  window.addEventListener("appinstalled", hideButtons);

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-pwa-install-button]");
    if (!button) return;
    if (deferredPrompt) {
      deferredPrompt.prompt();
      deferredPrompt.userChoice.finally(function () {
        deferredPrompt = null;
        hideButtons();
      });
    } else if (isIOS) {
      showIOSInstructions();
    }
  });

  function showIOSInstructions() {
    if (document.getElementById("pwa-ios-modal")) return;
    var overlay = document.createElement("div");
    overlay.id = "pwa-ios-modal";
    overlay.style.cssText =
      "position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;" +
      "background:rgba(0,0,0,0.5);padding:16px;";
    overlay.innerHTML =
      '<div style="background:#fff;border-radius:16px;max-width:320px;width:100%;padding:24px;' +
      'text-align:center;font-family:-apple-system,BlinkMacSystemFont,sans-serif;">' +
      '<p style="font-weight:600;font-size:16px;margin:0 0 12px;color:#1f1b17;">Instalar o Zellup</p>' +
      '<p style="font-size:14px;color:#594f45;line-height:1.5;margin:0 0 20px;">' +
      "Toque no ícone de compartilhar na barra do Safari e depois em " +
      '<strong>"Adicionar à Tela de Início"</strong>.</p>' +
      '<button id="pwa-ios-modal-close" style="background:#7d562d;color:#fff;border:0;' +
      'border-radius:999px;padding:10px 24px;font-weight:600;font-size:14px;">Entendi</button></div>';
    document.body.appendChild(overlay);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay || e.target.id === "pwa-ios-modal-close") overlay.remove();
    });
  }
})();
