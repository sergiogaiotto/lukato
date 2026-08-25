/* app.js — nucleo do console lukato.
 *
 * Publica `window.lukato`, o unico objeto global do console: os outros scripts
 * penduram as suas funcoes ali em vez de espalharem globais soltas. Nada de
 * bundler, nada de dependencia externa — ES2020 puro, carregado com `defer`.
 *
 * Responsabilidades:
 *   - formatacao pt-BR (numero, moeda, tokens) espelhando os filtros do Jinja;
 *   - `fetchJSON`, que entende o envelope de erro da API
 *     ({"error": {"code", "message"}}) e devolve sempre um Error com mensagem util;
 *   - toasts com auto-dismiss;
 *   - alternancia de tema, persistida em localStorage["lukato.theme"];
 *   - confirmacao de acoes destrutivas (`data-confirm`);
 *   - a cortina compartilhada pela sidebar e pela gaveta de contexto.
 *
 * Progressive enhancement: tudo aqui melhora uma pagina que ja funciona sem
 * JavaScript. Nenhuma funcionalidade obrigatoria depende deste arquivo.
 */

(function () {
  "use strict";

  var STORAGE_THEME = "lukato.theme";
  var TOAST_TIMEOUT = 4000;

  var numberFormats = {};

  /** Leitura tolerante do localStorage (modo privativo pode recusar). */
  function readStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  /** Escrita tolerante no localStorage. */
  function writeStorage(key, value) {
    try {
      window.localStorage.setItem(key, value);
      return true;
    } catch (error) {
      return false;
    }
  }

  function formatter(digits) {
    if (!numberFormats[digits]) {
      numberFormats[digits] = new Intl.NumberFormat("pt-BR", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
    }
    return numberFormats[digits];
  }

  /** Numero em pt-BR: ponto de milhar, virgula decimal. */
  function formatNumber(value, digits) {
    var amount = Number(value);
    if (!isFinite(amount)) {
      amount = 0;
    }
    return formatter(typeof digits === "number" ? digits : 0).format(amount);
  }

  /** Custo em dolar com cinco casas, como o filtro `money` do Jinja. */
  function formatMoney(value, digits) {
    return "US$ " + formatNumber(value, typeof digits === "number" ? digits : 5);
  }

  /** Contagem de tokens abreviada: 847, 1,2k, 3,4M. */
  function formatTokens(value) {
    var total = Math.round(Number(value) || 0);
    var sign = total < 0 ? "-" : "";
    var magnitude = Math.abs(total);
    var units = [
      [1e9, "B"],
      [1e6, "M"],
      [1e3, "k"],
    ];
    for (var i = 0; i < units.length; i += 1) {
      if (magnitude >= units[i][0]) {
        var reduced = magnitude / units[i][0];
        return sign + formatNumber(reduced, reduced >= 100 ? 0 : 1) + units[i][1];
      }
    }
    return sign + formatNumber(magnitude, 0);
  }

  /**
   * `fetch` de JSON com o contrato de erro da plataforma.
   *
   * Sucesso devolve o corpo ja convertido. Falha levanta um `Error` cuja
   * mensagem e a do envelope `{"error": {...}}`, com `code` e `status` anexados
   * — que e o que os `catch` da UI precisam para decidir o que mostrar.
   */
  async function fetchJSON(url, options) {
    var config = Object.assign({ credentials: "same-origin" }, options || {});
    config.headers = Object.assign({ Accept: "application/json" }, config.headers || {});
    if (config.body && typeof config.body !== "string") {
      config.headers["Content-Type"] = "application/json";
      config.body = JSON.stringify(config.body);
    }

    var response;
    try {
      response = await fetch(url, config);
    } catch (networkError) {
      var offline = new Error("Sem resposta do servidor. Verifique a conexão.");
      offline.code = "network_error";
      throw offline;
    }

    var payload = null;
    var text = await response.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (parseError) {
        payload = null;
      }
    }

    if (response.ok) {
      return payload;
    }

    var envelope = payload && payload.error ? payload.error : {};
    var failure = new Error(envelope.message || "Falha HTTP " + response.status + ".");
    failure.code = envelope.code || "http_error";
    failure.status = response.status;
    failure.details = envelope.details || {};
    throw failure;
  }

  /** Busca um fragmento HTML da propria origem; devolve o texto cru. */
  async function fetchHTML(url) {
    var response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
    });
    if (!response.ok) {
      var failure = new Error("Não foi possível carregar o conteúdo.");
      failure.status = response.status;
      throw failure;
    }
    return response.text();
  }

  /** Toast no canto inferior direito, com auto-dismiss em 4s. */
  function toast(message, tone) {
    var host = document.getElementById("lk-toasts");
    if (!host) {
      return;
    }
    var element = document.createElement("div");
    element.className = "lk-toast" + (tone ? " lk-toast--" + tone : "");
    element.setAttribute("role", "status");
    element.textContent = String(message);
    host.appendChild(element);

    window.setTimeout(function () {
      element.classList.add("is-leaving");
      window.setTimeout(function () {
        if (element.parentNode) {
          element.parentNode.removeChild(element);
        }
      }, 200);
    }, TOAST_TIMEOUT);
  }

  /* -- Tema ---------------------------------------------------------------- */

  function currentTheme() {
    var explicit = document.documentElement.getAttribute("data-theme");
    if (explicit === "dark" || explicit === "light") {
      return explicit;
    }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function paintThemeIcons(theme) {
    var icons = document.querySelectorAll("[data-theme-icon]");
    Array.prototype.forEach.call(icons, function (node) {
      node.hidden = node.getAttribute("data-theme-icon") !== theme;
    });
  }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    writeStorage(STORAGE_THEME, theme);
    paintThemeIcons(theme);
  }

  function setupTheme() {
    paintThemeIcons(currentTheme());
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        setTheme(currentTheme() === "dark" ? "light" : "dark");
      });
    });
  }

  /* -- Cortina compartilhada ------------------------------------------------ */

  /** Fecha sidebar em overlay e gaveta de contexto ao clicar fora ou no Esc. */
  function setupOverlay() {
    var overlay = document.getElementById("lk-overlay");
    if (!overlay) {
      return;
    }
    overlay.addEventListener("click", function () {
      document.dispatchEvent(new CustomEvent("lukato:dismiss"));
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        document.dispatchEvent(new CustomEvent("lukato:dismiss"));
      }
    });
  }

  /* -- Confirmacao e sessao -------------------------------------------------- */

  /** Qualquer elemento com `data-confirm` pede confirmacao antes de agir. */
  function setupConfirmations() {
    document.addEventListener(
      "click",
      function (event) {
        var trigger = event.target.closest("[data-confirm]");
        if (!trigger) {
          return;
        }
        if (!window.confirm(trigger.getAttribute("data-confirm"))) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );
  }

  /**
   * "Sair" limpa a credencial guardada neste navegador e volta ao cockpit.
   *
   * Com a autenticacao desligada nao ha sessao alguma: o link avisa em vez de
   * fingir que encerrou algo.
   */
  function setupLogout() {
    var links = document.querySelectorAll("[data-logout]");
    Array.prototype.forEach.call(links, function (link) {
      link.addEventListener("click", function (event) {
        if (link.getAttribute("aria-disabled") === "true") {
          event.preventDefault();
          toast("Autenticação desligada nesta instalação.", "warn");
          return;
        }
        try {
          window.localStorage.removeItem("lukato.token");
          window.sessionStorage.removeItem("lukato.token");
        } catch (error) {
          /* armazenamento indisponivel: nada a limpar */
        }
      });
    });
  }

  var api = {
    fetchJSON: fetchJSON,
    fetchHTML: fetchHTML,
    formatMoney: formatMoney,
    formatNumber: formatNumber,
    formatTokens: formatTokens,
    readStorage: readStorage,
    setTheme: setTheme,
    toast: toast,
    writeStorage: writeStorage,
  };

  window.lukato = Object.assign(window.lukato || {}, api);

  function boot() {
    setupTheme();
    setupOverlay();
    setupConfirmations();
    setupLogout();
    document.dispatchEvent(new CustomEvent("lukato:ready"));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
