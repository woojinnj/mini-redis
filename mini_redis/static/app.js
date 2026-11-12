'use strict';
const $ = id => document.getElementById(id);
let toastTimer;
const history = [];

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, signal: AbortSignal.timeout(12000) });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return { body, response };
}
function notify(message) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}
async function busy(action) {
  const buttons = [...document.querySelectorAll('main button')];
  buttons.forEach(button => { button.disabled = true; });
  try { await action(); }
  catch (error) { notify(error.message || '요청을 완료하지 못했습니다.'); }
  finally { buttons.forEach(button => { button.disabled = false; }); refresh(); }
}
function pretty(value) {
  if (value === null) return '(nil)';
  if (value === '') return '""  (빈 문자열)';
  if (typeof value !== 'string') return JSON.stringify(value, null, 2);
  try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
}
async function refresh() {
  try {
    const { body } = await api('/api/status');
    $('connection-dot').className = 'dot online';
    $('connection-text').textContent = '연결됨';
    $('key-count').textContent = body.keys.toLocaleString();
  } catch {
    $('connection-dot').className = 'dot offline';
    $('connection-text').textContent = '연결 안 됨';
    $('key-count').textContent = '—';
  }
  try {
    const { body } = await api('/stats');
    $('origin-count').textContent = body.origin_reads;
    $('hit-count').textContent = body.hits;
    $('miss-count').textContent = body.misses;
    $('hit-rate').textContent = body.hits + body.misses ? `${Math.round(body.hits / (body.hits + body.misses) * 100)}%` : '—';
  } catch { /* Connection indicator above reports server availability. */ }
}
function addHistory(args, result, failed = false) {
  history.unshift({ args, result, failed, time: new Date().toLocaleTimeString('ko-KR', { hour12: false }) });
  history.splice(20);
  $('history-count').textContent = history.length;
  $('history-body').replaceChildren();
  for (const item of history) {
    const row = document.createElement('tr');
    const summary = item.failed ? 'ERROR' : item.result.value === null ? '(nil)' : item.args[0] === 'GET' ? '반환 완료' : String(item.result.value);
    for (const text of [item.time, item.args[0], item.args[1] ?? '—', summary, item.failed ? '—' : `${item.result.elapsed_ms} ms`]) {
      const cell = document.createElement('td');
      cell.textContent = text;
      cell.title = text;
      row.append(cell);
    }
    $('history-body').append(row);
  }
}
async function execute(args) {
  $('result-command').textContent = `${args[0]}${args.length > 1 ? ' ' + args[1] : ''}`;
  $('result-time').textContent = '실행 중';
  try {
    const { body } = await api('/api/command', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ args }) });
    $('result').textContent = pretty(body.value);
    $('result-type').textContent = body.encoding === 'base64' ? 'Base64' : ({string: '문자열', integer: '정수', null: '값 없음'}[body.type]);
    $('result-time').textContent = `${body.elapsed_ms} ms`;
    $('result-detail').textContent = body.value === null ? '키 없음 또는 만료' : '';
    const wrap = $('result-wrap');
    wrap.classList.remove('flash');
    void wrap.offsetWidth;
    wrap.classList.add('flash');
    addHistory(args, body);
    return body;
  } catch (error) {
    $('result').textContent = error.message;
    $('result-type').textContent = 'ERROR';
    $('result-time').textContent = '실패';
    $('result-detail').textContent = '입력과 서버 연결을 확인하세요.';
    addHistory(args, {}, true);
    throw error;
  }
}
$('key-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(async () => {
    const args = ['SET', $('key').value, $('value').value];
    const ttl = $('ttl').value;
    if (ttl !== '') {
      if (!Number.isSafeInteger(Number(ttl)) || Number(ttl) <= 0) throw new Error('TTL은 양의 정수로 입력하세요.');
      args.push('EX', ttl);
    }
    await execute(args);
    notify('키를 저장했습니다.');
  });
});
$('get-key').addEventListener('click', () => busy(async () => {
  const key = $('key').value;
  const result = await execute(['GET', key]);
  if (result.value !== null && result.encoding === 'utf-8') $('value').value = result.value;
  const { body } = await api('/api/command', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ args: ['PTTL', key] }) });
  $('result-detail').textContent = body.value === -2 ? '키 없음 또는 만료' : body.value === -1 ? '만료 없음' : `남은 시간 ${(body.value / 1000).toFixed(2)}초`;
  $('ttl').value = body.value >= 0 ? Math.max(1, Math.ceil(body.value / 1000)) : '';
}));
$('delete-key').addEventListener('click', () => busy(async () => {
  const result = await execute(['DEL', $('key').value]);
  notify(result.value ? '키를 삭제했습니다.' : '삭제할 키가 없습니다.');
}));
$('snapshot').addEventListener('click', () => busy(async () => {
  await execute(['SAVE']);
  notify('스냅샷을 저장했습니다.');
}));
$('refresh').addEventListener('click', refresh);
for (const button of document.querySelectorAll('[data-tab]')) {
  button.addEventListener('click', () => {
    const tab = button.dataset.tab;
    for (const item of document.querySelectorAll('[data-tab]')) {
      item.classList.toggle('active', item === button);
      if (item === button) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
    }
    $('store-view').hidden = tab !== 'store';
    $('cache-view').hidden = tab !== 'cache';
    refresh();
  });
}
async function productRequest(cached) {
  const path = `/products/${$('product-id').value}?cache=${cached ? '1' : '0'}`;
  const start = performance.now();
  const { body, response } = await api(path);
  $('cache-path').textContent = `GET ${path}`;
  $('cache-state').textContent = response.headers.get('X-Cache');
  $('cache-result').textContent = pretty(body);
  $('cache-time').textContent = `${(performance.now() - start).toFixed(2)} ms`;
  $('cache-detail').textContent = { HIT: '캐시 응답', MISS: '원본 조회 후 캐시 저장', BYPASS: '원본 응답', ERROR: '캐시 연결 실패 · 원본 응답' }[response.headers.get('X-Cache')];
}
$('cached-get').addEventListener('click', () => busy(() => productRequest(true)));
$('uncached-get').addEventListener('click', () => busy(() => productRequest(false)));
$('product-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(async () => {
    const path = `/products/${$('product-id').value}`;
    const start = performance.now();
    const { body } = await api(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: $('product-name').value, price: Number($('product-price').value) }) });
    $('cache-path').textContent = `PUT ${path}`;
    $('cache-state').textContent = 'INVALIDATED';
    $('cache-result').textContent = pretty(body);
    $('cache-time').textContent = `${(performance.now() - start).toFixed(2)} ms`;
    $('cache-detail').textContent = '수정 완료 · 다음 조회에 최신 값 적용';
    notify('상품을 수정하고 기존 캐시를 무효화했습니다.');
  });
});
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
