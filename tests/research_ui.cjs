// Run with Playwright installed: node tests/research_ui.cjs
const {chromium}=require('playwright');
const fs=require('fs'),assert=require('node:assert/strict'),path=require('path');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 const page=await browser.newPage();const errors=[],reports=[];let delayReport=false;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://orbiview.test/**',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname==='/api/health')return route.fulfill({json:{status:'ready'}});
  if(u.pathname==='/api/analyze'){
   const result={status:'complete',result:{type:'analysis',region:'Jakarta',start_date:'2024-01-01',end_date:'2024-12-31',variables:['ndvi'],stats:{},layers:[],figures:{},conclusion:'Fixture'}};
   result.job_id='a'.repeat(32);
   return route.fulfill({contentType:'application/x-ndjson',body:JSON.stringify(result)+'\n'});
  }
  if(u.pathname==='/api/generate_report'){
   reports.push(route.request().postDataJSON());
   if(delayReport)await new Promise(r=>setTimeout(r,900));
   return route.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({status:'complete',filename:'b'.repeat(32)+'.pdf',title:'Jakarta’s Vegetation <2024>',download_name:'Jakarta_Vegetation_2024.pdf'})+'\n'}).catch(()=>{});
  }
  const file=u.pathname==='/'?path.join(root,'templates/index.html'):path.join(root,'public',u.pathname);
  if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
  return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.png')?'image/png':'text/html'});
 });
 await page.goto('http://orbiview.test/');await page.waitForFunction(()=>typeof L!=='undefined' && typeof Plotly!=='undefined');
 await page.locator('#researchBtn').click();await page.waitForTimeout(500);assert.equal(reports.length,0,'empty chat must not generate');
 await page.locator('#chatInput').fill('Analyze NDVI Jakarta in 2024');await page.locator('#sendBtn').click();
 await page.locator('.rac-name').waitFor();assert.equal(reports.length,1);
 assert.equal(await page.locator('.rac-name').textContent(),'Jakarta’s Vegetation <2024>');
 assert.equal(await page.locator('.rac-download-btn').getAttribute('download'),'Jakarta_Vegetation_2024.pdf');
 const saved=await page.evaluate(()=>{saveCurrentChat();return activeChatId});
 await page.evaluate(()=>clearChat());await page.locator('#researchBtn').click();await page.waitForTimeout(500);assert.equal(reports.length,1,'new chat must not reuse old context');
 await page.evaluate(id=>{toggleHistoryPanel();loadChat(id)},saved);assert.equal(await page.evaluate(()=>hasResearchContext()),true);
 await page.locator('#chatInput').fill('Analyze another region');await page.locator('#researchBtn').click();await page.waitForTimeout(300);assert.equal(reports.length,1,'draft must not generate previous report');
 await page.locator('#chatInput').fill('');await page.locator('#researchBtn').click();delayReport=true;await page.locator('#researchBtn').click();
 await page.waitForTimeout(100);assert.equal(reports.length,2);await page.evaluate(()=>clearChat());await page.waitForTimeout(1100);
 assert.equal(await page.locator('.rac-name').count(),0,'late report must not enter new chat');
 assert.equal(await page.evaluate(()=>_activeReportJobId),null);
 assert.deepEqual(errors,[]);console.log('PASS: empty chat, current analysis, descriptive title, new chat, restored chat, draft, cancelled late report, no JS errors');
 await browser.close();
})();
