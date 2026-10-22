const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'moqlab-browser-'));
  const chrome = spawn('google-chrome', ['--headless', '--no-sandbox', '--disable-gpu', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank']);
  let socket;
  try {
    const endpoint = await new Promise((resolve, reject) => {
      let logs = '';
      const timer = setTimeout(() => reject(new Error('Chrome startup timeout')), 15000);
      chrome.stderr.on('data', data => {
        logs += data;
        const match = logs.match(/DevTools listening on (ws:\/\/[^\s]+)/);
        if (match) {clearTimeout(timer); resolve(match[1]);}
      });
      chrome.on('exit', code => reject(new Error(`Chrome exited ${code}`)));
    });
    socket = new WebSocket(endpoint);
    await new Promise(resolve => socket.addEventListener('open', resolve, {once: true}));
    let id = 0;
    const pending = new Map();
    socket.addEventListener('message', ({data}) => {
      const message = JSON.parse(data);
      if (message.id && pending.has(message.id)) {
        const {resolve, reject, timer} = pending.get(message.id);
        pending.delete(message.id); clearTimeout(timer);
        message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
      }
    });
    function command(method, params = {}, sessionId) {
      return new Promise((resolve, reject) => {
        const key = ++id;
        const timer = setTimeout(() => {pending.delete(key); reject(new Error(`CDP timeout ${method}`));}, 15000);
        pending.set(key, {resolve, reject, timer});
        socket.send(JSON.stringify({id: key, method, params, sessionId}));
      });
    }
    const {targetId} = await command('Target.createTarget', {url: process.argv[2]});
    const {sessionId} = await command('Target.attachToTarget', {targetId, flatten: true});
    async function evaluate(expression) {
      const result = await command('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true}, sessionId);
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    }
    for (let i = 0; i < 100; ++i) {
      if (await evaluate('typeof manifest !== "undefined" && manifest?.simulation && typeof draft.experiment !== "undefined"')) break;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.match(await evaluate('document.querySelector("h1").textContent'), /simulation/);
    assert.equal(await evaluate('manifest.simulation'), true);
    await evaluate('document.querySelector("#global-config-buttons button").click()');
    assert.match(await evaluate('editor.textContent'), /Duration/);
    await evaluate(`(() => { const label = [...editor.querySelectorAll('label')].find(e=>e.textContent.startsWith('Duration S')); const input = label.parentElement.querySelector('input'); input.value='75'; input.dispatchEvent(new Event('change')); })()`);
    assert.equal(await evaluate('draft.experiment.duration_s'), 75);
    await evaluate(`document.querySelector('#undo').click()`);
    assert.equal(await evaluate('draft.experiment.duration_s'), 60);
    await evaluate(`document.querySelector('#redo').click()`);
    assert.equal(await evaluate('draft.experiment.duration_s'), 75);
    await evaluate(`([...editor.querySelectorAll('button')].find(e=>e.textContent==='Add events')).click()`);
    assert.equal(await evaluate('draft.experiment.events.length'), 1);
    await evaluate(`(() => { const select=editor.querySelector('select[aria-label="Item 1 type"]'); select.value='3'; select.dispatchEvent(new Event('change')); })()`);
    assert.equal(await evaluate('draft.experiment.events[0].kind'), 'flow');
    await evaluate(`(() => { const label=[...editor.querySelectorAll('label')].find(e=>e.textContent.startsWith('Origin')); const input=label.parentElement.querySelector('input'); input.value='origin'; input.dispatchEvent(new Event('change')); })()`);
    await evaluate('validate()');
    assert.equal(await evaluate('valid'), true);
    const text = await evaluate('(async()=>await (await yamlResponse()).text())()');
    assert.match(text, /kind: simulation/);
    assert.match(text, /duration_s: 75/);
    await evaluate('importYaml(' + JSON.stringify(text) + ')');
    assert.equal(await evaluate('draft.experiment.events[0].origin'), 'origin');
    assert.equal(await evaluate('draft.experiment.duration_s'), 75);
    assert.match(await evaluate('digest'), /^simulation-/);
    await evaluate(`commit(() => {draft.experiment.queues['router->switch'] = structuredClone(draft.experiment.queue); draft.experiment.routes=[{sender:'server',receiver:'client-a',forward:['server','router','switch','client-a'],reverse:['client-a','switch','router','server']}]; }); selected={kind:'node',role:'router',id:'router'}; renameNode('router','router','renamed-router')`);
    assert.equal(await evaluate(`Object.hasOwn(draft.experiment.queues, 'renamed-router->switch')`), true);
    assert.equal(await evaluate(`draft.experiment.routes[0].forward[1]`), 'renamed-router');
    await evaluate('validate()'); assert.equal(await evaluate('valid'), true);
    await evaluate('undo()');
    assert.equal(await evaluate(`Object.hasOwn(draft.experiment.queues, 'router->switch')`), true);
    console.log('PASS Chrome simulation forms, event variant, undo/redo, validation and YAML round trip');
  } finally {
    if (socket) socket.close();
    chrome.kill('SIGTERM');
    await new Promise(resolve => chrome.once('exit', resolve));
    fs.rmSync(profile, {recursive: true, force: true});
  }
})().catch(error => {console.error(error); process.exitCode=1;});
