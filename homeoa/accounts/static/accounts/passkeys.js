// Passkey ceremonies (PLT-02). Buttons with data-passkey="login" or "register" run them.
(function () {
  "use strict";

  function b64uToBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function bufToB64u(buf) {
    var bytes = new Uint8Array(buf), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function csrf() {
    var el = document.querySelector("input[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (data) { data._ok = r.ok; return data; });
    });
  }

  function creds(list) {
    return (list || []).map(function (c) {
      return { type: c.type, id: b64uToBuf(c.id), transports: c.transports };
    });
  }

  function attResponse(cred) {
    var r = cred.response;
    return {
      id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
      response: {
        clientDataJSON: bufToB64u(r.clientDataJSON),
        attestationObject: bufToB64u(r.attestationObject),
        transports: r.getTransports ? r.getTransports() : [],
      },
    };
  }

  function assertResponse(cred) {
    var r = cred.response;
    return {
      id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
      response: {
        clientDataJSON: bufToB64u(r.clientDataJSON),
        authenticatorData: bufToB64u(r.authenticatorData),
        signature: bufToB64u(r.signature),
        userHandle: r.userHandle ? bufToB64u(r.userHandle) : null,
      },
    };
  }

  function show(btn, msg) {
    var box = document.getElementById(btn.dataset.error);
    if (box) { box.textContent = msg; box.hidden = !msg; }
  }

  function finish(btn, data) {
    if (data.redirect) { window.location.assign(data.redirect); return; }
    show(btn, data.error || btn.dataset.failed);
    btn.disabled = false;
  }

  function login(btn) {
    return post(btn.dataset.options).then(function (o) {
      return navigator.credentials.get({ publicKey: {
        challenge: b64uToBuf(o.challenge), rpId: o.rpId, timeout: o.timeout,
        userVerification: o.userVerification, allowCredentials: creds(o.allowCredentials),
      } });
    }).then(function (cred) {
      return post(btn.dataset.finish, { credential: assertResponse(cred) });
    });
  }

  function register(btn) {
    var nameInput = document.getElementById(btn.dataset.name);
    return post(btn.dataset.options).then(function (o) {
      return navigator.credentials.create({ publicKey: {
        challenge: b64uToBuf(o.challenge), rp: o.rp, timeout: o.timeout,
        user: { id: b64uToBuf(o.user.id), name: o.user.name, displayName: o.user.displayName },
        pubKeyCredParams: o.pubKeyCredParams, attestation: o.attestation,
        authenticatorSelection: o.authenticatorSelection,
        excludeCredentials: creds(o.excludeCredentials),
      } });
    }).then(function (cred) {
      return post(btn.dataset.finish, {
        credential: attResponse(cred), name: nameInput ? nameInput.value : "",
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var supported = !!(window.PublicKeyCredential && navigator.credentials);
    document.querySelectorAll("[data-passkey]").forEach(function (btn) {
      if (!supported) { btn.hidden = true; return; }
      btn.hidden = false;
      btn.addEventListener("click", function () {
        btn.disabled = true;
        show(btn, "");
        var run = btn.dataset.passkey === "register" ? register : login;
        run(btn).then(function (data) { finish(btn, data); }, function () {
          show(btn, btn.dataset.failed);
          btn.disabled = false;
        });
      });
    });
  });
})();
