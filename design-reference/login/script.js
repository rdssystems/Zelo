// Zellup Login Screen Script
document.addEventListener('DOMContentLoaded', () => {
  const loginForm = document.getElementById('loginForm');
  const alertBanner = document.getElementById('alertBanner');
  const emailInput = document.getElementById('emailInput');
  const passwordInput = document.getElementById('passwordInput');

  const btnDefault = document.getElementById('btnDefault');
  const btnError = document.getElementById('btnError');
  const btnMobile = document.getElementById('btnMobile');

  // Handle Form Submit to show error state if credentials are provided or empty for testing
  if (loginForm) {
    loginForm.addEventListener('submit', (e) => {
      e.preventDefault();
      // Show error state alert banner as specified in user prompt
      alertBanner.classList.remove('hidden');
    });
  }

  // State Switcher Handlers for Desktop Default, Desktop Error, Mobile Simulation
  if (btnDefault) {
    btnDefault.addEventListener('click', () => {
      setActiveStateBtn(btnDefault);
      alertBanner.classList.add('hidden');
      document.body.classList.remove('simulated-mobile');
      if (emailInput) emailInput.value = '';
      if (passwordInput) passwordInput.value = '';
    });
  }

  if (btnError) {
    btnError.addEventListener('click', () => {
      setActiveStateBtn(btnError);
      alertBanner.classList.remove('hidden');
      document.body.classList.remove('simulated-mobile');
      if (emailInput && !emailInput.value) emailInput.value = 'contato@salaoexemplo.com.br';
      if (passwordInput && !passwordInput.value) passwordInput.value = 'senha1234';
    });
  }

  if (btnMobile) {
    btnMobile.addEventListener('click', () => {
      setActiveStateBtn(btnMobile);
      alertBanner.classList.add('hidden');
      document.body.classList.add('simulated-mobile');
    });
  }

  function setActiveStateBtn(activeBtn) {
    [btnDefault, btnError, btnMobile].forEach(btn => {
      if (btn) btn.classList.remove('active');
    });
    if (activeBtn) activeBtn.classList.add('active');
  }
});
