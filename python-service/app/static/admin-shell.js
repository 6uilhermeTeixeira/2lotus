(() => {
  const body = document.body;
  const opener = document.querySelector('[data-sidebar-open]');
  const closers = document.querySelectorAll('[data-sidebar-close]');
  if (!opener) return;

  const setOpen = open => {
    body.classList.toggle('sidebar-open', open);
    opener.setAttribute('aria-expanded', String(open));
    if (open) document.querySelector('.app-sidebar-close')?.focus();
    else opener.focus();
  };

  opener.addEventListener('click', () => setOpen(true));
  closers.forEach(control => control.addEventListener('click', () => setOpen(false)));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && body.classList.contains('sidebar-open')) setOpen(false);
  });
  document.querySelectorAll('.app-nav-link').forEach(link => {
    link.addEventListener('click', () => body.classList.remove('sidebar-open'));
  });
})();
