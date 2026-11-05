/* End-to-end checks create their own server, synthetic orders and temporary storage. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const crypto=require('node:crypto');
const root=path.resolve(__dirname,'..');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'kwazar-browser-'));
const token=crypto.randomBytes(32).toString('hex');
let child,browser;
const env={...process.env};for(const k of Object.keys(env))if(k.startsWith('KWAZAR_'))delete env[k];
Object.assign(env,{KWAZAR_DATA_DIR:temp,KWAZAR_ADMIN_TOKEN:token,KWAZAR_PAYMENTS_ENABLED:'0'});
function start(){return new Promise((resolve,reject)=>{child=spawn('python3',['server.py','--bind','127.0.0.1','--port','0'],{cwd:root,env,stdio:['ignore','pipe','pipe']});const timer=setTimeout(()=>reject(Error('Test server timeout')),10000);child.on('error',reject);child.stdout.on('data',chunk=>{const m=chunk.toString().match(/port (\d+)/);if(m){clearTimeout(timer);resolve(`http://127.0.0.1:${m[1]}`)}});child.stderr.on('data',()=>{});child.once('exit',code=>{if(code)reject(Error('Test server failed'))})})}
function stop(){return new Promise(resolve=>{if(!child||child.exitCode!==null)return resolve();child.once('exit',resolve);child.kill('SIGTERM')})}
(async()=>{
 const catalog=JSON.parse(fs.readFileSync(path.join(root,'catalog.json'),'utf8'));const first=catalog.find(p=>p.socket==='H7');const firstId=first.id;
 let base=await start();browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 for(const width of [320,390,768,1440]){
  await page.setViewportSize({width,height:1000});await page.goto(base);await page.locator('.product-card').nth(5).waitFor();await page.evaluate(()=>document.fonts.ready);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`Page overflow at ${width}`);
  if(width<801){await page.getByRole('button',{name:'Открыть меню',exact:true}).click();assert(await page.locator('#main-nav').isVisible());await page.locator('#main-nav a').first().click();assert(!(await page.locator('#main-nav').isVisible()))}
  console.log(`PASS: ${width}px responsive layout, navigation, catalog`);
 }
 await page.locator(`[data-favorite=${firstId}]`).click();await page.locator('#favorites-open').click();assert.equal(await page.locator('.product-card').count(),1);await page.reload();await page.locator('.product-card').nth(5).waitFor();assert.equal(await page.locator('[data-count=favorites]').innerText(),'1');
 await page.getByRole('tab',{name:'HB-серия',exact:true}).click();assert.equal(await page.locator('.product-card').count(),Math.min(12,catalog.filter(p=>p.series==='hb').length));
 await page.getByRole('tab',{name:'Все товары',exact:true}).click();await page.locator('#search').fill('9005');assert.equal(await page.locator('.product-card').count(),catalog.filter(p=>p.alias.includes('9005')).length);await page.locator('#search').fill('xyz');assert(await page.locator('#empty-state').isVisible());await page.locator('#reset-filters').click();
 for(const id of catalog.slice(0,4).map(p=>p.id))await page.locator(`[data-compare=${id}]`).click();assert.equal(await page.locator('[data-count=compare]').innerText(),'3');await page.locator('#compare-open').click();assert.equal(await page.locator('.compare-table thead th').count(),4);await page.locator('#compare-dialog [data-close]').click();await page.locator('#compare-clear').click();
 await page.locator('#load-more').click();assert.equal(await page.locator('.product-card').count(),24);
 await page.locator('#product-type').selectOption('mini');assert((await page.locator('.product-title').first().innerText()).includes('Мини-линзы'));await page.locator('#product-type').selectOption('all');await page.locator('#sort').selectOption('power-desc');assert((await page.locator('.product-article').first().innerText()).includes('130'));await page.locator('#sort').selectOption('default');
 await page.locator(`[data-detail=${firstId}]`).click();const before=await page.locator('#gallery-image').getAttribute('src');await page.locator('#gallery-next').click();assert.notEqual(await page.locator('#gallery-image').getAttribute('src'),before);await page.getByRole('tab',{name:'Характеристики',exact:true}).click();assert((await page.locator('#detail-text').innerText()).includes(String(first.wbId)));assert.equal(await page.locator('#detail-dialog .detail-wb').getAttribute('href'),first.url);await page.getByRole('tab',{name:'Совместимость',exact:true}).click();assert((await page.locator('#detail-text').innerText()).includes('Сверь маркировку'));await page.locator('#detail-add').click();await page.locator('#detail-dialog [data-close]').click();await page.locator('#cart-open').click();await page.locator(`[data-quantity=${firstId}][data-step="1"]`).click();assert.equal(await page.locator('.quantity>span').innerText(),'2');await page.locator(`[data-remove=${firstId}]`).click();assert(await page.locator('#cart-to-catalog').isVisible());await page.locator('#cart-dialog [data-close]').click();
 console.log('PASS: persisted favorites, search, category tabs, compare limit, detail tabs, cart quantity/removal');
 await page.locator('#finder-form [name=make]').fill('Тестовая марка');await page.locator('#finder-form [name=model]').fill('Тестовая модель');await page.locator('#finder-form [name=year]').fill('2020');await page.locator('#finder-form button[type=submit]').click();assert(await page.locator('#cart-dialog').isVisible());
 await page.locator('#checkout-form [name=name]').fill('Тест');await page.locator('#checkout-form [name=contact]').fill('bad');await page.locator('#checkout-form [name=consent]').check();await page.locator('#checkout-submit').click();assert((await page.locator('#checkout-error').innerText()).includes('корректный'));
 await page.locator('#checkout-form [name=contact]').fill('browser-test@example.invalid');await page.locator('#checkout-submit').click();await page.getByRole('heading',{name:'Заявка сохранена',exact:true}).waitFor();
 const orderHref=await page.locator('#order-success a.button').getAttribute('href');await page.locator('#order-success a.button').click();await page.getByText('Заявка получена',{exact:true}).waitFor();assert((await page.locator('#customer-order').innerText()).includes('Тестовая модель'));
 const admin=await browser.newPage();await admin.goto(base+'/admin.html');await admin.locator('#admin-token').fill(token);await admin.getByRole('button',{name:'Войти в кабинет'}).click();await admin.getByRole('button',{name:'Открыть заявку',exact:true}).click();await admin.locator('#quote-form [name=description]').fill('Тестовый комплект, данные не настоящие');await admin.locator('#quote-form [name=amount]').fill('5900.99');await admin.getByRole('button',{name:'Подтвердить стоимость'}).click();await admin.getByText('Стоимость подтверждена',{exact:false}).first().waitFor();
 await page.locator('#refresh-order').click();await page.getByText('Стоимость подтверждена',{exact:true}).waitFor();assert(await page.locator('#pay-order').isDisabled());assert((await page.locator('#customer-order').innerText()).includes('5 900,99'));
 await admin.locator('#admin-logout').click();assert(await admin.locator('#admin-login').isVisible());
 console.log('PASS: vehicle request, invalid contact rejection, saved order, owner login/quote/logout, customer quote, payment disabled');
 await stop();base=await start();await page.goto(base+'/'+orderHref);await page.getByText('Стоимость подтверждена',{exact:true}).waitFor();console.log('PASS: orders and quote persist across server restart');
 assert.deepEqual(errors,[]);console.log('PASS: no JavaScript errors');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{if(browser)await browser.close();await stop();fs.rmSync(temp,{recursive:true,force:true})});
