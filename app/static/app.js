// No patient identifiers or medication lists are stored in localStorage/cookies.
document.addEventListener('DOMContentLoaded', () => {
  const button = document.getElementById('check-interactions');
  if (!button) return;
  button.addEventListener('click', async () => {
    const area = document.getElementById('medicine-list');
    const result = document.getElementById('interaction-result');
    const names = [...new Set(area.value.split(/[\n;]+/).map(x => x.trim()).filter(Boolean))];
    result.classList.add('alert');
    if (names.length < 2) { result.textContent = 'Необходимо указать как минимум два препарата. Медицинская проверка пока недоступна.'; return; }
    if (names.length > 20) { result.textContent = 'Максимум 20 названий в одном запросе.'; return; }
    button.disabled = true;
    try {
      const response = await fetch('/api/interactions/check', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({medicine_names: names}), cache: 'no-store',
      });
      if (!response.ok) throw new Error('Не удалось обработать запрос');
      const data = await response.json();
      result.replaceChildren();
      const bold = document.createElement('strong');
      bold.textContent = 'Проверка не выполнена';
      const p = document.createElement('p');
      p.textContent = data.message;
      const count = document.createElement('p');
      count.textContent = 'Препаратов в списке: ' + data.input_names.length + '. Список не сохранён.';
      result.append(bold, p, count);
    } catch (e) { result.textContent = 'Ошибка обработки запроса. Проверка взаимодействий не выполнена.'; }
    finally { button.disabled = false; }
  });
});

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('quick-search');
  const suggestions = document.getElementById('search-suggestions');
  if (!input || !suggestions) return;
  let timer = null;
  let controller = null;

  function clear() {
    suggestions.hidden = true;
    suggestions.replaceChildren();
  }

  input.addEventListener('input', () => {
    clearTimeout(timer);
    if (controller) controller.abort();
    const query = input.value.trim();
    if (query.length < 2) { clear(); return; }
    timer = setTimeout(async () => {
      controller = new AbortController();
      try {
        const response = await fetch('/api/search?q=' + encodeURIComponent(query), {
          signal: controller.signal, cache: 'no-store'
        });
        if (!response.ok || input.value.trim() !== query) return;
        const found = await response.json();
        clear();
        if (!found.length) return;
        for (const item of found) {
          const a = document.createElement('a');
          a.className = 'search-result';
          const id = Number(item.id);
          if (!Number.isSafeInteger(id) || id <= 0) continue;
          a.href = '/medicines/' + id;
          const name = document.createElement('strong');
          name.textContent = String(item.name);
          const detail = document.createElement('small');
          detail.textContent = 'Код ' + String(item.code) + ' · Ящик №' + String(item.drawer);
          a.append(name, detail);
          suggestions.append(a);
        }
        suggestions.hidden = !suggestions.childElementCount;
      } catch (e) {
        if (e.name !== 'AbortError') clear();
      }
    }, 200);
  });
  input.addEventListener('keydown', e => { if (e.key === 'Escape') clear(); });
  document.addEventListener('click', e => {
    if (!suggestions.contains(e.target) && e.target !== input) clear();
  });
});
