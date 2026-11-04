/* Local screenshot zoom controls; images never leave PharmDesk. */
(() => {
  "use strict";
  const viewport = document.querySelector("[data-review-viewport]");
  const photo = document.querySelector("[data-review-photo]");
  if (!viewport || !photo) return;
  const plus = document.querySelector("[data-zoom-in]");
  const minus = document.querySelector("[data-zoom-out]");
  const reset = document.querySelector("[data-zoom-reset]");
  const label = document.querySelector("[data-zoom-label]");
  let zoom = 1;
  const render = () => {
    photo.style.width = (zoom * 100) + "%";
    photo.style.maxWidth = "none";
    photo.style.height = "auto";
    if (label) label.textContent = Math.round(zoom * 100) + "%";
    if (minus) minus.disabled = zoom <= 1;
    if (plus) plus.disabled = zoom >= 4;
  };
  if (plus) plus.addEventListener("click", () => {
    zoom = Math.min(4, Math.round((zoom + 0.25) * 100) / 100);
    render();
  });
  if (minus) minus.addEventListener("click", () => {
    zoom = Math.max(1, Math.round((zoom - 0.25) * 100) / 100);
    render();
  });
  if (reset) reset.addEventListener("click", () => {
    zoom = 1;
    viewport.scrollTo({ left: 0, top: 0 });
    render();
  });
  render();
})();
