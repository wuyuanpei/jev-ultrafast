(() => {
  const rendered = e => !!e && !e.closest('[hidden],[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) &&
    e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
  const onscreen = e => {
    if (!rendered(e)) return false;
    const r = e.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  };
  const text = e => rendered(e) ? e.innerText.trim() : '';
  const read = selector => text(document.querySelector(selector));
  const cache = window.__jevFast;
  const node = e => cache?.ids.get(e) ?? null;
  const article = document.querySelector('.article-content');
  const wiki = document.querySelector('#mw-content-text .mw-parser-output');
  const stay = document.querySelector('.detail-copy');
  const results = [...document.querySelectorAll('#content_left .result, #content_left .result-op, #content_left .c-container')]
    .filter(e => onscreen(e) && e.querySelector('h3 a'));
  const finish = document.querySelector('#finish h4');
  const visible = page?.text || '';
  const dialogs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"]')].filter(onscreen);
  const challenge = /请完成安全验证|请拖动滑块|访问过于频繁|异常访问|verify you are human|checking your browser|access denied/i;
  let environmentBlock = '';
  if (location.protocol === 'chrome-error:' || /ERR_(NAME_NOT_RESOLVED|CONNECTION|TIMED_OUT|CERT)/.test(visible))
    environmentBlock = 'Browser network error';
  else if (challenge.test(visible)) environmentBlock = 'Visible access or CAPTCHA challenge';
  else if (dialogs.some(e => /登录|log\s*in|sign\s*in/i.test(text(e)) &&
      e.querySelector('input[type="password"],input[autocomplete="one-time-code"]')))
    environmentBlock = 'Login required';
  let travel = null;
  if (location.pathname === '/fixture.html' && typeof searched === 'boolean' && typeof query === 'string') {
    travel = {searched, query, category, free};
  }
  const ctrip = /(^|\.)ctrip\.com$/.test(location.hostname);
  const rowSelector = '.flight-item,.flight-box,.flight-card,[class*="flight-item"],[class*="flightItem"],[class*="flight-card"]';
  const rows = ctrip ? [...document.querySelectorAll(rowSelector)].filter(e => onscreen(e) &&
    /\d{1,2}:\d{2}/.test(text(e)) && /[¥￥]|\d+起/.test(text(e)) && /[A-Z0-9]{2}\s?\d{3,4}/.test(text(e))) : [];
  const selectedDirect = ctrip && [...document.querySelectorAll('input:checked,[aria-checked="true"],[aria-selected="true"]')]
    .some(e => rendered(e) && /仅直飞|只看直飞|直飞/.test(text(e.closest('label') || e.parentElement)));
  const formValues = ctrip ? [...document.querySelectorAll('input:not([type="password"]),select')]
    .filter(onscreen).map(e => e.value).join('\n') : '';
  return {
    url: location.href, environment_block: environmentBlock,
    article_visible: onscreen(article), article_heading: read('.article-content h1'), article_text: text(article).slice(0, 5000),
    wiki_visible: onscreen(wiki), wiki_heading: read('#firstHeading'), wiki_text: text(wiki).slice(0, 5000),
    baidu_results: results.length,
    baidu_result_nodes: results.flatMap(e => [...e.querySelectorAll('h3 a')].map(node)).filter(n => n !== null),
    pagination: read('.pager .current'), books_visible: [...document.querySelectorAll('article.product_pod')].filter(onscreen).length,
    checkboxes: [...document.querySelectorAll('#checkboxes input[type="checkbox"]')].filter(rendered)
      .map(e => ({checked: e.checked, node: node(e)})),
    travel, stay_visible: onscreen(stay), stay_heading: read('.detail-copy h1'), stay_text: text(stay).slice(0, 2500),
    loading_finish_visible: onscreen(finish), loading_finish_text: text(finish),
    ctrip_controls: ctrip ? (visible.slice(0, 20000) + '\n' + formValues) : '',
    ctrip_direct_selected: selectedDirect,
    flight_rows: rows.slice(0, 30).map(e => ({text: text(e).slice(0, 1500),
      direct: /直飞/.test(text(e)) && !/经停|中转/.test(text(e))})),
  };
})()
