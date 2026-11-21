// openajar.com: the copy buttons and the take over demo. No trackers, nothing sent anywhere.
(() => {
  for (const button of document.querySelectorAll('[data-copy]')) {
    const word = button.querySelector('.copy-word');
    let timer;
    button.addEventListener('click', async () => {
      const text = button.getAttribute('data-copy');
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // Older browsers, or a page without clipboard access: select the line so it can be copied by hand.
        const code = button.parentElement.querySelector('code');
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        return;
      }
      button.setAttribute('data-done', '');
      if (word) word.textContent = 'Copied';
      button.setAttribute('aria-label', 'Copied the install line');
      clearTimeout(timer);
      timer = setTimeout(() => {
        button.removeAttribute('data-done');
        if (word) word.textContent = 'Copy';
        button.setAttribute('aria-label', 'Copy the install line');
      }, 2000);
    });
  }

  const stage = document.getElementById('stage');
  const take = document.getElementById('take');
  const status = document.getElementById('take-status');
  if (stage && take) {
    take.addEventListener('click', () => {
      const you = !stage.hasAttribute('data-you');
      stage.toggleAttribute('data-you', you);
      take.textContent = you ? 'Hand back' : 'Take over';
      if (status) status.textContent = you ? 'You have control.' : 'Claude Code has control again.';
    });
  }
})();
