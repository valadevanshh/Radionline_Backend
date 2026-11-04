// UI e2e against the isolated local stack (API :8011 on throwaway SQLite, web :3011).
const { chromium } = require('playwright');
const API = 'http://127.0.0.1:8011/api', WEB = 'http://127.0.0.1:3011';
const SHOTS = process.env.SHOTS || '.';
const results = [];
const check = (name, ok, extra = '') => { results.push([name, !!ok]); console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  -> ' + String(extra).slice(0, 300))); };
async function api(path, opts = {}, token) {
  const r = await fetch(API + path, { ...opts, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) } });
  let body = null; try { body = await r.json(); } catch {}
  return { status: r.status, body };
}
async function uiLogin(page, email, pw) {
  await page.goto(WEB + '/login');
  await page.fill('input[placeholder="you@example.com"]', email);
  await page.fill('input[placeholder="Enter your password"]', pw);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/dashboard/, { timeout: 60000 });
}
(async () => {
  const sa = (await api('/auth/login', { method: 'POST', body: JSON.stringify({ email: 'sa@e2e.local', password: 'SaPass123!' }) })).body.access_token;
  const cen = await api('/centers', { method: 'POST', body: JSON.stringify({ centerName: 'BETA SCAN', email: 'beta@e2e.local', contactNumber: '9000000001', username: 'beta@e2e.local', password: 'Beta1234!' }) }, sa);
  const centerId = cen.body.id;
  const browser = await chromium.launch();
  // ---- Super Admin creates a Manager in the UI
  const ctxA = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const p = await ctxA.newPage();
  await uiLogin(p, 'sa@e2e.local', 'SaPass123!');
  check('SA sidebar shows Managers', await p.waitForSelector('a[href="/dashboard/managers"]', { state: 'attached', timeout: 20000 }).then(() => true).catch(() => false));
  await p.goto(WEB + '/dashboard/managers');
  await p.click('[data-testid="add-manager"]');
  const form = p.locator('[data-testid="manager-form"]');
  await form.locator('input').nth(0).fill('Mira Manager');
  await form.locator('input[type="email"]').fill('mira@e2e.local');
  await form.locator('input[type="password"]').fill('MiraPass123!');
  await p.check('[data-testid="perm-reports-read"]');
  await p.check('[data-testid="perm-centers-write"]');
  check('ticking Write auto-ticks Read', await p.isChecked('[data-testid="perm-centers-read"]'));
  await p.screenshot({ path: SHOTS + '/1_sa_add_manager.png' });
  await form.locator('button[type="submit"]').click();
  await p.waitForSelector('text=mira@e2e.local', { timeout: 20000 });
  await p.screenshot({ path: SHOTS + '/2_sa_managers_list.png' });
  const list = (await api('/managers', {}, sa)).body;
  const mira = list.find((m) => m.email === 'mira@e2e.local');
  check('manager saved with chosen rights', mira && mira.permissions.centers.write && mira.permissions.reports.read && !mira.permissions.doctors.read && !mira.permissions.centers.delete, JSON.stringify(mira));

  // ---- Manager sees only granted pages
  const ctxM = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const m = await ctxM.newPage();
  await uiLogin(m, 'mira@e2e.local', 'MiraPass123!');
  await m.waitForSelector('a[href="/dashboard/radiology"]', { state: 'attached', timeout: 20000 }).catch(() => {});
  check('manager nav: All Reports shown', await m.locator('a[href="/dashboard/all-reports"]').count() > 0);
  check('manager nav: Centers shown', await m.locator('a[href="/dashboard/radiology"]').count() > 0);
  check('manager nav: Doctors hidden', await m.locator('a[href="/dashboard/doctors"]').count() === 0);
  check('manager nav: Managers hidden', await m.locator('a[href="/dashboard/managers"]').count() === 0);
  await m.screenshot({ path: SHOTS + '/3_manager_nav.png' });
  await m.goto(WEB + '/dashboard/doctors');
  await m.waitForURL((u) => u.pathname === '/dashboard', { timeout: 20000 }).catch(() => {});
  check('manager direct URL to Doctors is redirected', new URL(m.url()).pathname === '/dashboard', m.url());
  await m.goto(WEB + '/dashboard/managers');
  await m.waitForURL((u) => u.pathname === '/dashboard', { timeout: 20000 }).catch(() => {});
  check('manager direct URL to Managers is redirected', new URL(m.url()).pathname === '/dashboard', m.url());

  // ---- Manager edits a centre -> approval
  await m.goto(WEB + '/dashboard/radiology');
  await m.waitForSelector(`[data-testid="center-edit-${centerId}"]`, { timeout: 30000 });
  check('manager sees Edit centre', true);
  check('manager has no Delete centre (no delete right)', await m.locator('[title="Delete Radiology Center"]').count() === 0);
  check('manager sees Freeze (write)', await m.locator(`[data-testid="center-freeze-${centerId}"]`).count() > 0);
  await m.click(`[data-testid="center-edit-${centerId}"]`);
  await m.waitForSelector('text=Edit Radiology Diagnostic Center');
  check('edit modal prefilled', (await m.inputValue('input[placeholder="Enter Center Name"]')) === 'BETA SCAN');
  await m.fill('input[placeholder="Enter Center Name"]', 'BETA SCAN EDITED BY MANAGER');
  await m.screenshot({ path: SHOTS + '/4_manager_edit_centre.png' });
  await m.locator('form button[type="submit"]').click();
  await m.waitForTimeout(2000);
  await m.screenshot({ path: SHOTS + '/5_manager_after_submit.png' });
  let c1 = (await api('/centers', {}, sa)).body.find((c) => c.id === centerId);
  check('centre NOT changed before SA approval', c1.centerName === 'BETA SCAN', c1.centerName);
  const pend = (await api('/approvals?status=PENDING', {}, sa)).body.filter((a) => a.managerId === 'mira@e2e.local');
  check('one UPDATE_CENTER pending for SA', pend.length === 1 && pend[0].actionType === 'UPDATE_CENTER', JSON.stringify(pend).slice(0, 200));
  check('pending edit keeps login password out of payload', !pend[0].payload.password, JSON.stringify(pend[0].payload));

  // ---- SA approves in the UI
  await p.goto(WEB + '/dashboard/approvals');
  await p.waitForSelector('text=Mira Manager', { timeout: 30000 });
  await p.screenshot({ path: SHOTS + '/6_sa_approvals.png' });
  const approveBtn = p.getByRole('button', { name: 'Approve', exact: true }).first();
  await approveBtn.click();
  
  await p.waitForTimeout(2500);
  c1 = (await api('/centers', {}, sa)).body.find((c) => c.id === centerId);
  check('centre edit live after SA approves in UI', c1.centerName === 'BETA SCAN EDITED BY MANAGER', c1.centerName);
  check('centre login password still works after edit', (await api('/auth/login', { method: 'POST', body: JSON.stringify({ email: 'beta@e2e.local', password: 'Beta1234!' }) })).status === 200);

  // ---- SA edits a centre in the UI: immediate
  await p.goto(WEB + '/dashboard/radiology');
  await p.waitForSelector(`[data-testid="center-edit-${centerId}"]`, { timeout: 30000 });
  check('SA sees Delete centre', await p.locator('[title="Delete Radiology Center"]').count() > 0);
  await p.click(`[data-testid="center-edit-${centerId}"]`);
  await p.fill('input[placeholder="Enter Center Name"]', 'BETA BY SA');
  await p.locator('form button[type="submit"]').click();
  await p.waitForTimeout(2000);
  c1 = (await api('/centers', {}, sa)).body.find((c) => c.id === centerId);
  check('SA centre edit applies immediately', c1.centerName === 'BETA BY SA', c1.centerName);
  await p.screenshot({ path: SHOTS + '/7_sa_after_edit.png' });

  // ---- SA removes centres write: manager UI loses edit after session refresh
  await api(`/managers/${mira.id}`, { method: 'PUT', body: JSON.stringify({ permissions: { reports: { read: true }, centers: { read: true } } }) }, sa);
  await m.goto(WEB + '/dashboard/radiology');
  await m.waitForTimeout(2500);
  await m.reload();
  await m.waitForTimeout(3000);
  check('after SA revokes write, manager has no Edit button', await m.locator(`[data-testid="center-edit-${centerId}"]`).count() === 0);
  await m.screenshot({ path: SHOTS + '/8_manager_read_only.png' });

  await browser.close();
  const fails = results.filter((r) => !r[1]);
  console.log(`\n${results.length - fails.length}/${results.length} passed`);
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
