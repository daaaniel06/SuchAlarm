// Swiping a row with a finger, mouse or trackpad: right saves, left hides (rows.js runs the action).

import { afterSwing, band } from './logic.js';
import { isFlat, markOpened } from './ui.js';
import { openSheet } from './sheet.js';
import { render, renderLater } from './page.js';
import { rows, settle, swiped } from './rows.js';

// The row follows the finger, mouse or trackpad; the action behind it arms past the threshold
// and runs only when the gesture is released.
const SWIPE_ARM = 80;
export let swipe = null,
  wheel = null;
let wheelLock = 0,
  swallowClick = false;
// Input can arrive faster than the screen refreshes (120 wheel events a second); the row is drawn
// once per frame, as a GPU layer, and classes change only when the state really changes.
function drag(g, dx) {
  g.dx = band(dx);
  if (!g.frame) g.frame = requestAnimationFrame(() => paint(g));
}
function paint(g) {
  g.frame = 0;
  g.row.style.transform = `translate3d(${g.dx}px,0,0)`;
  const side = Math.sign(g.dx),
    armed = Math.abs(g.dx) >= SWIPE_ARM;
  if (side !== g.side) {
    g.li.classList.toggle('to-right', side > 0);
    g.li.classList.toggle('to-left', side < 0);
    g.side = side;
  }
  if (armed !== g.armed) {
    g.li.classList.toggle('armed', armed);
    g.armed = armed;
  }
}
function release(g) {
  if (g.frame) {
    cancelAnimationFrame(g.frame);
    g.frame = 0;
  }
  setTimeout(() => {
    if (renderLater) render();
  }, 320); // a refresh that arrived during the gesture
  g.li.classList.remove('swiping'); // transitions on again: the row moves smoothly from here
  if (Math.abs(g.dx) >= SWIPE_ARM) swiped(g.row.dataset.offer, g.li, g.row, g.dx > 0);
  else settle(g.li, g.row);
}
document.addEventListener('pointerdown', e => {
  const row = e.target.closest('.asset-row[data-offer]');
  if (!row || e.button > 0 || wheel) return;
  swipe = { row, li: row.parentElement, id: e.pointerId, x: e.clientX, y: e.clientY, dx: 0, active: false };
});
document.addEventListener('pointermove', e => {
  if (!swipe || e.pointerId !== swipe.id) return;
  const dx = e.clientX - swipe.x,
    dy = e.clientY - swipe.y;
  if (!swipe.active) {
    if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) {
      swipe = null;
      return;
    } // the page scrolls
    if (Math.abs(dx) < 10) return;
    swipe.active = true;
    swipe.row.setPointerCapture(e.pointerId);
    swipe.li.classList.add('swiping');
  }
  drag(swipe, dx);
});
function endSwipe(e) {
  if (!swipe || e.pointerId !== swipe.id) return;
  const g = swipe;
  swipe = null;
  if (!g.active) return;
  swallowClick = true;
  setTimeout(() => {
    swallowClick = false;
  }, 0);
  release(g);
}
document.addEventListener('pointerup', endSwipe);
document.addEventListener('pointercancel', endSwipe);
// Two-finger swipes on a Mac trackpad arrive as horizontal wheel events. Safari keeps the
// "fingers down / lifted" events from pages (WebKit drops zero-delta wheel events before the DOM),
// so the gesture is read from what does arrive:
// - The row sticks to the fingers. Holding still never ends the gesture and nothing springs back.
// - Lifting while moving leaves the system's after-swing: a long run of steadily fading events in
//   one direction. Once that is certain, the decision is made at the point where the fingers left,
//   not where the swing carried the row. Chrome says so directly (WheelEvent.momentum).
// - Lifting after holding still sends nothing at all. The next real sign of the hand, moving the
//   pointer or clicking, counts as the release; a long pause is the last resort.
// The action runs only on that release, and only if the row is past the threshold then.
const IDLE = 120,
  LAST_RESORT = 4000,
  SWING_COUNTED_MS = 100;
function letGoAt(g, position) {
  if (g.done) return;
  g.done = true;
  clearTimeout(g.idle);
  clearTimeout(g.lastResort);
  if (wheel === g) wheel = null;
  wheelLock = Date.now() + 150; // the rest of the swing belongs to this gesture
  g.dx = band(position);
  release(g);
}
function waitForHand(g) {
  // The stream stopped without an after-swing: the fingers rest on the pad, or left it standing still.
  g.waiting = true;
  g.lastResort = setTimeout(() => letGoAt(g, g.sum), LAST_RESORT);
}
document.addEventListener(
  'pointermove',
  e => {
    const g = wheel;
    if (!g || !g.waiting) return;
    if (Math.hypot(e.clientX - g.px, e.clientY - g.py) > 3) letGoAt(g, g.sum);
  },
  true,
);
document.addEventListener(
  'pointerdown',
  () => {
    if (wheel?.waiting) letGoAt(wheel, wheel.sum);
  },
  true,
);
let pointerAt = { x: 0, y: 0 };
document.addEventListener('pointermove', e => {
  pointerAt = { x: e.clientX, y: e.clientY };
});
document.addEventListener(
  'wheel',
  e => {
    const step = e.deltaMode === 1 ? 16 : 1,
      sideways = Math.abs(e.deltaX) > Math.abs(e.deltaY);
    if (Date.now() < wheelLock) {
      if (sideways) {
        e.preventDefault();
        wheelLock = Date.now() + 150;
      }
      return;
    }
    let g = wheel;
    if (g?.waiting && !sideways) {
      letGoAt(g, g.sum);
      return;
    } // fingers back on the pad to scroll the page
    if (!g) {
      const row = e.target.closest?.('.asset-row[data-offer]');
      if (!row || !sideways || swipe) return;
      g = wheel = { row, li: row.parentElement, sum: 0, dx: 0, events: [], px: pointerAt.x, py: pointerAt.y };
      g.li.classList.add('swiping');
    }
    e.preventDefault();
    if (e.momentum === true) {
      letGoAt(g, g.sum);
      return;
    } // Chrome: the fingers just left the pad
    g.waiting = false;
    clearTimeout(g.lastResort);
    const move = -e.deltaX * step; // natural scrolling: the row moves with the fingers
    g.sum += move;
    drag(g, g.sum);
    g.events.push({ t: e.timeStamp, d: move, after: g.sum });
    if (g.events.length > 240) g.events.splice(0, g.events.length - 240); // the swing is judged on the recent past only
    if (typeof e.momentum !== 'boolean') {
      const swing = afterSwing(g.events);
      // Lifted while moving. Certainty needs 150 ms of swing, but only its first 100 ms count, like a
      // flick: a small swipe that happens to end in a swing does not glide past the threshold.
      if (swing >= 0) {
        const counted = g.events.filter((x, k) => k < swing || x.t - g.events[swing].t <= SWING_COUNTED_MS);
        letGoAt(g, counted.at(-1).after);
        return;
      }
    }
    clearTimeout(g.idle);
    g.idle = setTimeout(() => waitForHand(g), IDLE);
  },
  { passive: false },
);
document.addEventListener(
  'click',
  e => {
    const row = e.target.closest('.asset-row[data-offer]');
    if (!row) return;
    if (swallowClick) {
      e.preventDefault();
      return;
    }
    const entry = rows.get(row.dataset.offer);
    if (!entry) return;
    // A flat opens its detail sheet; a modified click still opens the portal in a new tab.
    if (isFlat(entry.s) && !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)) {
      e.preventDefault();
      openSheet(entry.o);
    } else markOpened(entry.o);
  },
  true,
);
