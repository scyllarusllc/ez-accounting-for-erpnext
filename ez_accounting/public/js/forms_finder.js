(() => {
  const $ = id => document.getElementById(id);
  const form = $('finder-form');
  if (!form) return;
  const fields = {q:'finder-q', company:'finder-company', category:'finder-category',
    document_from:'finder-from', document_to:'finder-to', expiry_from:'finder-expiry-from', expiry_to:'finder-expiry-to'};
  let page = 0, busy = false;
  const endpoint = '/api/method/ez_accounting.www.forms.user.finder.';
  async function call(method, params = {}) {
    const url = new URL(endpoint + method, window.location.origin);
    Object.entries(params).forEach(([key, value]) => { if (value !== '') url.searchParams.set(key, value); });
    const response = await fetch(url, {credentials:'same-origin', cache:'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data._server_messages ? 'Search request failed or access denied.' : 'Search request failed.');
    return data.message;
  }
  function option(select, value) {
    const row = document.createElement('option'); row.value = value; row.textContent = value; select.append(row);
  }
  function render(row) {
    const box = document.createElement('article'); box.className = 'finder-result';
    const title = document.createElement('h3'); title.textContent = row.title || row.name; box.append(title);
    const meta = document.createElement('div'); meta.className = 'finder-meta';
    meta.textContent = [row.company, row.category, row.document_date && `Document: ${row.document_date}`,
      row.expiry_date && `Expires: ${row.expiry_date}`].filter(Boolean).join(' · '); box.append(meta);
    if (row.description) { const desc = document.createElement('p'); desc.className = 'finder-desc';
      desc.textContent = row.description; box.append(desc); }
    if (row.tags) { const tags = document.createElement('div'); tags.className = 'finder-meta'; tags.textContent = row.tags; box.append(tags); }
    if (row.attachment && /^\/(?:private\/)?files\//.test(row.attachment)) {
      const link = document.createElement('a'); link.href = row.attachment; link.textContent = 'Open file ↗';
      link.target = '_blank'; link.rel = 'noopener'; box.append(link);
    }
    $('finder-results').append(box);
  }
  async function search(reset = true) {
    if (busy) return;
    busy = true;
    if (reset) { page = 0; $('finder-results').replaceChildren(); }
    $('finder-status').textContent = 'Searching…'; $('finder-more').hidden = true;
    try {
      const params = Object.fromEntries(Object.entries(fields).map(([key, id]) => [key, $(id).value]));
      const result = await call('search', {...params, page});
      result.rows.forEach(render);
      $('finder-status').textContent = $('finder-results').childElementCount + ' document(s) shown';
      if (!result.rows.length && page === 0) $('finder-status').textContent = 'No documents found.';
      $('finder-more').hidden = !result.has_more;
      if (result.has_more) page++;
    } catch (error) { $('finder-status').textContent = error.message; }
    finally { busy = false; }
  }
  form.addEventListener('submit', event => { event.preventDefault(); search(true); });
  $('finder-clear').addEventListener('click', () => { form.reset(); search(true); });
  $('finder-more').addEventListener('click', () => search(false));
  call('options').then(data => {
    data.companies.forEach(name => option($('finder-company'), name));
    data.categories.forEach(name => option($('finder-category'), name));
    search(true);
  }).catch(error => { $('finder-status').textContent = error.message; });
})();
