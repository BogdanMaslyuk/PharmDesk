document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('import-form');
  if (!form) return;
  const previewButton = document.getElementById('preview-import');
  const applyButton = document.getElementById('apply-import');
  const report = document.getElementById('import-report');
  const fileInput = form.querySelector('input[type=file]');
  let previewReady = false;

  function message(text, error = false) {
    report.replaceChildren();
    const textNode = document.createElement('p');
    textNode.textContent = text;
    report.appendChild(textNode);
    report.classList.toggle('alert', error);
  }

  function showResult(data) {
    report.replaceChildren();
    const title = document.createElement('strong');
    title.textContent = data.mode === 'preview' ? 'Предварительная проверка' : 'Импорт завершён';
    report.appendChild(title);

    const summary = document.createElement('p');
    summary.textContent = 'Строк: ' + data.rows + ' · Ящиков: ' + data.drawers
      + ' · Новых: ' + data.created + ' · Обновится: ' + data.updated
      + ' · Пропущено: ' + data.skipped;
    report.appendChild(summary);

    if (data.errors && data.errors.length) {
      const label = document.createElement('strong');
      label.textContent = 'Проблемы в строках:';
      report.appendChild(label);
      const list = document.createElement('ul');
      for (const text of data.errors) {
        const li = document.createElement('li');
        li.textContent = text;
        list.appendChild(li);
      }
      report.appendChild(list);
      if (data.more_errors) {
        const more = document.createElement('p');
        more.textContent = 'И ещё ошибок: ' + data.more_errors;
        report.appendChild(more);
      }
    }
    const note = document.createElement('p');
    note.textContent = data.message;
    report.appendChild(note);
    report.classList.toggle('alert', !!data.skipped);
  }

  function resetPreview() {
    previewReady = false;
    applyButton.disabled = true;
    message('Проверьте файл: после изменения параметров нужно повторить предпросмотр.');
  }
  form.addEventListener('change', resetPreview);
  form.addEventListener('input', resetPreview);

  async function send(action) {
    if (!fileInput.files.length) {
      message('Выберите файл CSV или XLSX.', true);
      return;
    }
    previewButton.disabled = true;
    applyButton.disabled = true;
    message(action === 'preview' ? 'Проверка файла...' : 'Сохранение данных...');
    try {
      const response = await fetch('/manage/import/' + action, {
        method: 'POST', body: new FormData(form), cache: 'no-store',
        credentials: 'same-origin'
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || data.detail || 'Ошибка обработки файла.');
      showResult(data);
      if (action === 'preview') {
        previewReady = (data.valid > 0);
        applyButton.disabled = !previewReady;
      } else {
        previewReady = false;
        applyButton.disabled = true;
      }
    } catch (error) {
      message('Ошибка: ' + error.message, true);
      previewReady = false;
    } finally {
      previewButton.disabled = false;
      if (!previewReady) applyButton.disabled = true;
    }
  }
  previewButton.addEventListener('click', () => send('preview'));
  applyButton.addEventListener('click', () => {
    if (!previewReady) return;
    if (!window.confirm('Сохранить изменения в локальной базе?')) return;
    send('apply');
  });
});
