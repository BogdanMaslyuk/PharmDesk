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
