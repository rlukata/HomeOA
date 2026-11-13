// Link a bank account with Stripe.js (Financial Connections). Values come from the
// button's data-* attributes: the CSP forbids inline scripts.
(function () {
  var btn = document.getElementById("collect-bank");
  if (!btn) return;
  var errorBox = document.getElementById("collect-bank-error");
  function showError(message) {
    errorBox.textContent = message || btn.dataset.errorText;
    errorBox.hidden = false;
  }
  btn.addEventListener("click", async function () {
    var d = btn.dataset;
    errorBox.hidden = true;
    btn.disabled = true;
    try {
      var stripe = Stripe(d.publishableKey);
      var res = await stripe.collectBankAccountForSetup({
        clientSecret: d.clientSecret,
        params: {
          payment_method_type: "us_bank_account",
          payment_method_data: { billing_details: { name: d.billingName, email: d.billingEmail } },
        },
      });
      if (res.error) {
        showError(res.error.message);
        return;
      }
      window.location.reload();
    } catch (e) {
      // Stripe.js failed to load, or the network dropped: never fail silently.
      showError();
    } finally {
      btn.disabled = false;
    }
  });
})();
