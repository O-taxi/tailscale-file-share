const state = { downloadable: 1, uploaded: 1 };
const perPage = 10;
const api = (area, name, action) => `/api/files/${area}/${encodeURIComponent(name)}/${action}`;
const bytes = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 1048576).toFixed(1)} MiB`;
const date = value => new Intl.DateTimeFormat('ja-JP', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));

function makeButton(label, style, handler) { const element = document.createElement('button'); element.type = 'button'; element.textContent = label; element.className = style; element.onclick = handler; return element; }
function makeLink(label, href) { const element = document.createElement('a'); element.textContent = label; element.href = href; element.className = 'button secondary'; return element; }
async function request(url, options) { const response = await fetch(url, options); const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.error || '操作に失敗しました。'); return data; }

function render(area, data) {
  const target = document.querySelector(`#${area}-list`);
  target.replaceChildren();
  if (!data.items.length) { const paragraph = document.createElement('p'); paragraph.className = 'empty'; paragraph.textContent = area === 'downloadable' ? '公開中のファイルはありません。' : 'アップロード済みファイルはありません。'; target.append(paragraph); return; }
  const table = document.createElement('table'), thead = document.createElement('thead'), tbody = document.createElement('tbody');
  thead.innerHTML = '<tr><th>ファイル名</th><th>更新日時</th><th>サイズ</th><th>操作</th></tr>';
  for (const item of data.items) {
    const row = document.createElement('tr');
    for (const value of [item.name, date(item.modifiedAt), bytes(item.size)]) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
    const actions = document.createElement('td'); actions.className = 'actions';
    if (item.previewable) actions.append(makeButton('プレビュー', 'secondary', () => preview(area, item)));
    actions.append(makeLink('ダウンロード', api(area, item.name, 'download')));
    if (area === 'uploaded') actions.append(makeButton('削除', 'danger', () => removeUpload(item.name)));
    row.append(actions); tbody.append(row);
  }
  table.append(thead, tbody); target.append(table);
  const pages = Math.max(1, Math.ceil(data.total / data.perPage)), pager = document.createElement('div'), prev = makeButton('前へ', 'secondary', () => load(area, state[area] - 1)), next = makeButton('次へ', 'secondary', () => load(area, state[area] + 1)), info = document.createElement('span');
  prev.disabled = data.page <= 1; next.disabled = data.page >= pages; info.textContent = `${data.total} 件中 ${data.page} / ${pages} ページ`; pager.className = 'pager'; pager.append(prev, info, next); target.append(pager);
}

async function load(area, page = 1) { const data = await request(`/api/files/${area}?page=${page}&per_page=${perPage}`); state[area] = data.page; render(area, data); }
async function removeUpload(name) { if (!confirm(`「${name}」を削除しますか？`)) return; try { await request(`/api/files/uploaded/${encodeURIComponent(name)}`, { method: 'DELETE' }); await load('uploaded', state.uploaded); } catch (error) { alert(error.message); } }
async function preview(area, item) {
  const dialog = document.querySelector('#preview-dialog'), box = document.querySelector('#preview-area'), url = api(area, item.name, 'preview');
  box.replaceChildren(); document.querySelector('#preview-title').textContent = item.name;
  if (item.mimeType.startsWith('image/')) { const element = document.createElement('img'); element.id = 'preview-content'; element.src = url; element.alt = item.name; box.append(element); }
  else if (item.mimeType === 'application/pdf') { const element = document.createElement('iframe'); element.id = 'preview-content'; element.src = url; element.title = item.name; element.sandbox = 'allow-downloads'; box.append(element); }
  else { const element = document.createElement('pre'); element.textContent = '読み込み中…'; box.append(element); try { const response = await fetch(url); if (!response.ok) throw new Error('プレビューを取得できません。'); element.textContent = await response.text(); } catch (error) { element.textContent = error.message; } }
  dialog.showModal();
}

document.querySelector('#close-preview').onclick = () => document.querySelector('#preview-dialog').close();
document.querySelector('#upload-form').onsubmit = async event => {
  event.preventDefault(); const input = document.querySelector('#upload-files'), notice = document.querySelector('#notice'), submit = event.currentTarget.querySelector('button');
  if (!input.files.length) return;
  const form = new FormData(); for (const file of input.files) form.append('files', file, file.name);
  submit.disabled = true; notice.className = 'notice'; notice.textContent = 'アップロード中…';
  try { const data = await request('/api/files/upload', { method: 'POST', body: form }); notice.className = 'notice ok'; notice.textContent = `${data.uploaded.join('、')} をアップロードしました。`; event.currentTarget.reset(); await load('uploaded', 1); }
  catch (error) { notice.className = 'notice error'; notice.textContent = error.message; }
  finally { submit.disabled = false; }
};
Promise.all([load('downloadable'), load('uploaded')]).catch(error => { const notice = document.querySelector('#notice'); notice.className = 'notice error'; notice.textContent = error.message; });
