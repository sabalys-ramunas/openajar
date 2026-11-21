// openajar.com: the scene of the agent at work, the Take over button, the copy buttons, the recording and
// the arrivals on scroll. No trackers, nothing sent anywhere. The page reads fine without this file.
(() => {
  const root = document.documentElement;
  root.classList.add('js');
  document.body.classList.remove('no-js');
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');

  // A few still stars in the night sky (never twinkling: nothing on the page moves all the time).
  const stars = document.getElementById('stars');
  if (stars) {
    let seed = 7;
    const rnd = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646;
    const frag = document.createDocumentFragment();
    for (let i = 0; i < 70; i++) {
      const s = document.createElement('i');
      const y = rnd() ** 1.6 * 100;
      s.style.cssText = `left:${(rnd() * 100).toFixed(2)}%;top:${y.toFixed(2)}%;opacity:${(0.15 + rnd() * 0.6 * (1 - y / 140)).toFixed(2)};transform:scale(${(0.5 + rnd() * 0.8).toFixed(2)})`;
      frag.appendChild(s);
    }
    stars.appendChild(frag);
  }

  // Copy buttons.
  for (const button of document.querySelectorAll('[data-copy]')) {
    const word = button.querySelector('.copy-word');
    let timer;
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(button.getAttribute('data-copy'));
      } catch {
        // No clipboard access: select the line so it can be copied by hand.
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

  // Arrivals on scroll.
  const rising = document.querySelectorAll('.rise');
  if ('IntersectionObserver' in window && !reduce.matches) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
    }, { rootMargin: '0px 0px -12% 0px' });
    rising.forEach((el) => io.observe(el));
  } else {
    rising.forEach((el) => el.classList.add('in'));
  }

  // The real recording: a still until asked, then the clip (loaded only then).
  const clip = document.getElementById('clip');
  const play = document.getElementById('play');
  const clipImg = document.getElementById('clip-img');
  if (clip && play && clipImg) {
    const still = clipImg.getAttribute('src');
    play.addEventListener('click', () => {
      const on = !clip.classList.contains('playing');
      clip.classList.toggle('playing', on);
      clipImg.src = on ? clipImg.dataset.clip : still;
      play.lastChild.textContent = on ? 'Stop' : 'Play the recording';
    });
  }

  // ------------------------------------------------------------------ The scene
  const hero = document.getElementById('hero-scene');
  const scene = document.getElementById('scene');
  if (!hero || !scene) return;
  const screen = scene.querySelector('.screen');
  const $ = (id) => document.getElementById(id);
  const ptr = $('ptr'), ring = $('ring'), flash = $('flash');
  const doing = $('s-doing'), clock = $('s-time');
  const doneN = $('done-n'), bar = $('bar'), notes = $('notes'), ph = $('notes-ph'), typed = $('typed');
  const term = $('term'), cmd1 = $('cmd1'), l1 = $('l1'), l2 = $('l2');
  const toast1 = $('toast1'), toast2 = $('toast2');
  const ticks = [...scene.querySelectorAll('[data-tick]')];
  const yours = scene.querySelector('.yours');
  const browser = scene.querySelector('.browser');
  const NOTE = 'Ready for Friday. Checked on a phone too.';

  let paused = false;
  let onScreen = true;
  const live = new Set();
  const free = () => !paused && onScreen && !document.hidden;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function wait(ms) {
    let left = ms;
    while (left > 0) {
      while (!free()) await sleep(120);
      const step = Math.min(left, 80);
      await sleep(step);
      left -= step;
    }
  }
  async function run(el, frames, opts) {
    while (!free()) await sleep(120);
    const a = el.animate(frames, opts);
    live.add(a);
    try { await a.finished; } catch { /* cancelled */ }
    live.delete(a);
  }
  const setPaused = (p) => {
    paused = p;
    for (const a of live) (p ? a.pause() : a.play());
  };

  // The pointer, in pixels from the screen's corner, with the arrow's tip on the target.
  let at = { x: 0, y: 0 };
  const em = () => parseFloat(getComputedStyle(screen).fontSize);
  const place = (p) => { at = p; ptr.style.transform = `translate3d(${p.x}px, ${p.y}px, 0)`; };
  const spot = (el, fx = 0.5, fy = 0.5) => {
    const s = screen.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const e = em();
    return { x: r.left - s.left + r.width * fx - 0.36 * e, y: r.top - s.top + r.height * fy - 0.2 * e };
  };
  async function move(el, fx, fy, ms = 760) {
    const from = at;
    const to = spot(el, fx, fy);
    place(to);
    await run(ptr, [
      { transform: `translate3d(${from.x}px, ${from.y}px, 0)` },
      { transform: `translate3d(${to.x}px, ${to.y}px, 0)` },
    ], { duration: ms, easing: 'cubic-bezier(0.45, 0, 0.15, 1)' });
  }
  async function click() {
    ptr.classList.add('press');
    const e = em();
    const x = at.x + 0.36 * e, y = at.y + 0.2 * e;
    run(ring, [
      { transform: `translate3d(${x}px, ${y}px, 0) scale(0.3)`, opacity: 0.9 },
      { transform: `translate3d(${x}px, ${y}px, 0) scale(1.25)`, opacity: 0 },
    ], { duration: 480, easing: 'cubic-bezier(0.2, 0, 0, 1)' });
    await wait(130);
    ptr.classList.remove('press');
  }
  async function type(el, text, ms) {
    for (const ch of text) {
      el.textContent += ch;
      await wait(ms + (ch === ' ' ? ms : 0));
    }
  }
  let count = 3;
  const setCount = (n) => { count = n; doneN.textContent = String(n); bar.style.transform = `scaleX(${n / 6})`; };
  const say = (text, time) => { doing.textContent = text; if (time) clock.textContent = time; };

  function reset() {
    ticks.forEach((li) => li.classList.remove('done'));
    yours.classList.remove('left');
    setCount(3);
    notes.classList.remove('focus');
    ph.hidden = false;
    typed.textContent = '';
    term.classList.remove('show');
    cmd1.textContent = '';
    l1.classList.remove('on');
    l2.classList.remove('on');
    toast1.classList.remove('show');
    toast2.classList.remove('show');
    say('is using the browser', '22:14');
  }

  function finalState() {
    ticks.forEach((li) => li.classList.add('done'));
    setCount(5);
    yours.classList.add('left');
    ph.hidden = true;
    typed.textContent = NOTE;
    term.classList.add('show');
    cmd1.textContent = 'npm test';
    l1.classList.add('on');
    l2.classList.add('on');
    toast2.classList.add('show');
    say('is done', '22:41');
    requestAnimationFrame(() => place(spot(yours.querySelector('.t'), 0.75, 0.9)));
  }

  async function loop() {
    for (;;) {
      reset();
      place(spot(browser, 0.62, 0.42));
      await run(browser, [{ opacity: 0 }, { opacity: 1 }], { duration: 500, easing: 'ease-out' });
      await wait(700);
      for (const li of ticks) {
        await move(li.querySelector('.box'), 0.5, 0.55);
        await wait(140);
        await click();
        li.classList.add('done');
        setCount(count + 1);
        await wait(620);
      }
      say('is writing a note');
      await move(notes, 0.08, 0.42, 820);
      await click();
      notes.classList.add('focus');
      ph.hidden = true;
      await wait(250);
      await type(typed, NOTE, 42);
      await wait(500);
      notes.classList.remove('focus');

      say('is running the tests', '22:31');
      term.classList.add('show');
      await move(term, 0.86, 0.9, 900);
      await wait(300);
      await type(cmd1, 'npm test', 80);
      await wait(900);
      l1.classList.add('on');
      await wait(250);
      l2.classList.add('on');
      await wait(1100);

      say('kept a screenshot', '22:38');
      run(flash, [{ opacity: 0 }, { opacity: 0.55 }, { opacity: 0 }], { duration: 420, easing: 'ease-out' });
      await wait(200);
      toast1.classList.add('show');
      await wait(1500);
      say('opened a pull request', '22:41');
      toast2.classList.add('show');
      await wait(1300);
      say('is done');
      yours.classList.add('left');
      await move(yours.querySelector('.t'), 0.75, 0.9, 900);
      await wait(3600);
      await run(browser, [{ opacity: 1 }, { opacity: 0 }], { duration: 400, easing: 'ease-in' });
      term.classList.remove('show');
      toast1.classList.remove('show');
      toast2.classList.remove('show');
      await wait(450);
    }
  }

  // Take over: the agent's hands come off, the scene stops where it is. Hand back: it carries on.
  const take = $('take');
  const status = $('take-status');
  take.addEventListener('click', () => {
    const you = !hero.classList.contains('you');
    hero.classList.toggle('you', you);
    scene.classList.toggle('you', you);
    take.textContent = you ? 'Hand back' : 'Take over';
    setPaused(you);
    if (status) status.textContent = you ? 'You have control.' : 'Claude Code has control again.';
  });

  if ('IntersectionObserver' in window) {
    new IntersectionObserver((entries) => {
      onScreen = entries[0].isIntersecting;
      for (const a of live) (free() ? a.play() : a.pause());
    }).observe(scene);
  }
  document.addEventListener('visibilitychange', () => { for (const a of live) (free() ? a.play() : a.pause()); });

  if (reduce.matches || !ptr.animate) {
    finalState();
  } else {
    loop();
  }
})();
