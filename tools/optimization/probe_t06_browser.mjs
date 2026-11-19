// Adapted from the independent draft probe; this runner is implementation verification.
import fs from 'node:fs/promises'
import path from 'node:path'
import http from 'node:http'
import {createRequire} from 'node:module'

const [distArg,outputArg,pwArg]=process.argv.slice(2)
const dist=path.resolve(distArg),output=path.resolve(outputArg)
await fs.mkdir(output,{recursive:false})
const {chromium}=createRequire(import.meta.url)(path.resolve(pwArg))
const requests=[],errors=[],results=[]
let mode='normal',reads=0,owner=1,logged=true,releaseHeld=null
const user=()=>({id:owner,username:'fixture-user-'+owner,is_admin:false,has_credentials:true,credential_count:1,created_at:'2026-11-01T00:00:00Z'})
const task=(id,status='succeeded')=>({task_id:id,kind:'stop',status,credential_id:owner,account_id:'111111111111',
  region:'us-east-1',resource_name:'RESOURCE-'+id,current_step:'stop',steps:[{sequence:1,name:'stop',status:status==='reconciliation_required'?'unknown':status,
  operation_id:status==='succeeded'?'operation-'+id:null,error:status==='reconciliation_required'?'fixture-unknown':null}],
  error:status==='failed'?'fixture-failed':null,result:{marker:'RESULT-'+id},created_at:'2026-11-19T00:00:00Z',
  can_resume:false,can_cancel:status==='queued'})
async function api(req,res,url){
  let body='';for await(const chunk of req)body+=chunk
  const event={method:req.method,path:url.pathname,owner,body,idempotency_key:req.headers['idempotency-key']};requests.push(event)
  let data,status=200
  if(url.pathname==='/api/auth/me'){if(!logged){status=401;data={detail:'logged out'}}else data=user()}
  else if(url.pathname==='/api/auth/logout'){logged=false;data={ok:true}}
  else if(url.pathname==='/api/auth/login'){owner=2;logged=true;data={user:user()}}
  else if(url.pathname==='/api/credentials')data={has_credentials:true,items:[{id:1,account_label:'ALPHA',is_default:true,account_id:'111111111111',access_key_masked:'AKIA***'}]}
  else if(url.pathname==='/api/v2/instances')data={instances:[{name:'web',region:'us-east-1',state:'running',credential_id:1,is_static_ip:false}],scopes:[],refreshing:false}
  else if(url.pathname==='/api/traffic/summary')data={year_month:'2026-11',instances:[],by_region:[]}
  else if(url.pathname==='/api/v2/tasks/by-key')data=task('recovered')
  else if(url.pathname==='/api/v2/tasks'&&req.method==='POST'){status=202;data=task('submitted','queued')}
  else if(url.pathname==='/api/v2/tasks')data=mode==='states'?['queued','running','succeeded','failed','partial','reconciliation_required','resolved'].map(s=>task(s,s)):[task('list-user-'+owner)]
  else if(url.pathname.startsWith('/api/v2/tasks/')){
    const id=url.pathname.split('/')[4]
    if(mode==='held'&&id==='old'){
      await new Promise(resolve=>{releaseHeld=resolve})
      data=task('old')
    }else if(mode==='progress') data=task(id,++reads<3?'running':'succeeded')
    else data=task(id)
  }else{status=501;data={detail:'unhandled fixture'}}
  if(res.destroyed)return
  res.writeHead(status,{'content-type':'application/json','cache-control':'no-store'})
  res.end(JSON.stringify(data))
}
const server=http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://127.0.0.1')
    if(url.pathname.startsWith('/api/'))return await api(req,res,url)
    let target=path.resolve(dist,decodeURIComponent(url.pathname).replace(/^\/+/,'' )||'index.html')
    if(!target.startsWith(dist+path.sep))throw new Error('outside dist')
    let content
    try{content=await fs.readFile(target)}catch{target=path.join(dist,'index.html');content=await fs.readFile(target)}
    res.writeHead(200,{'content-type':{'.html':'text/html','.js':'text/javascript','.css':'text/css'}[path.extname(target)]||'application/octet-stream'})
    res.end(content)
  }catch(error){res.writeHead(500);res.end(String(error))}
})
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
const origin='http://127.0.0.1:'+server.address().port
const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
const contexts=[]
async function open(url){
  const context=await browser.newContext({viewport:{width:1440,height:1100},locale:'zh-CN'})
  contexts.push(context)
  const page=await context.newPage()
  page.on('pageerror',error=>errors.push(error.message))
  await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort())
  await page.goto(origin+url)
  await page.getByRole('heading',{name:'任务中心',exact:true}).waitFor()
  return page
}
async function run(name,fn){
  try{results.push({scenario:name,...await fn()})}catch(error){results.push({scenario:name,passed:false,error:String(error)})}
}
try{
  await run('instance_write_submits_task_with_key_and_opens_progress',async()=>{
    mode='normal';const page=await open('/tasks')
    await page.getByRole('link',{name:'实例',exact:true}).click()
    await page.getByRole('button',{name:/关\s*机/}).click()
    await page.getByText('RESOURCE-submitted',{exact:false}).first().waitFor()
    const post=requests.find(r=>r.path==='/api/v2/tasks'&&r.method==='POST')
    return {passed:!!post?.idempotency_key&&JSON.parse(post.body).kind==='stop'&&page.url().endsWith('/tasks/submitted')}
  })
  await run('direct_task_reload_retains_id',async()=>{
    mode='normal';const page=await open('/tasks/reload')
    await page.getByText('RESOURCE-reload',{exact:false}).first().waitFor()
    await page.reload()
    await page.getByText('RESULT-reload',{exact:false}).waitFor()
    const count=requests.filter(r=>r.path==='/api/v2/tasks/reload').length
    return {passed:count>=2,detail_gets:count}
  })
  await run('polls_running_until_confirmed',async()=>{
    mode='progress';reads=0;const page=await open('/tasks/progress')
    await page.getByText('执行中',{exact:true}).first().waitFor()
    await page.getByText('已确认完成',{exact:true}).first().waitFor({timeout:10000})
    const callsAtTerminal=reads
    await page.waitForTimeout(1800)
    return {passed:reads===callsAtTerminal&&callsAtTerminal>=3,reads,stopped_polling:reads===callsAtTerminal}
  })
  await run('all_task_states_distinct',async()=>{
    mode='states';const page=await open('/tasks')
    await page.getByText('待核对，资源锁保留',{exact:true}).waitFor()
    const labels=['已接受，等待执行','执行中','已确认完成','明确失败','部分完成','待核对，资源锁保留','已人工决议，AWS 结果未确认']
    const checks=await Promise.all(labels.map(async text=>({text,visible:await page.getByText(text,{exact:true}).first().isVisible()})))
    await page.screenshot({path:path.join(output,'task-states.png'),fullPage:true})
    await page.close()
    return {passed:checks.every(c=>c.visible),checks}
  })
  await run('late_detail_after_navigating_list',async()=>{
    mode='held';const page=await open('/tasks/old')
    await page.waitForFunction(()=>document.body.innerText.includes('全部任务'))
    await page.getByRole('link',{name:'全部任务',exact:true}).click()
    await page.getByText('RESOURCE-list-user-1',{exact:false}).first().waitFor()
    if(releaseHeld)releaseHeld()
    await page.waitForTimeout(300)
    return {passed:!(await page.locator('body').innerText()).includes('RESOURCE-old'),url:page.url()}
  })
  await run('pending_intent_restores_after_reload',async()=>{
    mode='normal';const page=await open('/tasks')
    await page.evaluate(()=>localStorage.setItem('lightsail-task-intents:1',JSON.stringify([{key:'fixture-lost-response',slot:'fixture-slot',created_at:'2026-11-19T00:00:00Z'}])))
    await page.reload()
    await page.getByRole('button',{name:'按原提交标识找回'}).click()
    await page.waitForFunction(()=>JSON.parse(localStorage.getItem('lightsail-task-intents:1')).length===0)
    return {passed:requests.some(r=>r.path==='/api/v2/tasks/by-key'),pending_removed:true}
  })
  await run('late_old_user_task_after_login',async()=>{
    mode='held';releaseHeld=null;owner=1;logged=true
    const page=await open('/tasks/old')
    await page.getByText('退出',{exact:false}).click()
    await page.getByLabel('用户名',{exact:true}).fill('fixture-user-2')
    await page.getByLabel('密码',{exact:true}).fill('FictionalLoginPass123!')
    await page.getByRole('button',{name:/登\s*录/}).click()
    await page.getByText('fixture-user-2',{exact:true}).waitFor()
    await page.getByRole('link',{name:'任务中心',exact:true}).click()
    await page.getByText('RESOURCE-list-user-2',{exact:false}).first().waitFor()
    if(releaseHeld)releaseHeld()
    await page.waitForTimeout(300)
    const text=await page.locator('body').innerText()
    return {passed:!text.includes('RESOURCE-old')&&text.includes('RESOURCE-list-user-2')}
  })
}finally{
  if(releaseHeld)releaseHeld()
  for(const context of contexts)await context.close()
  await browser.close()
  await new Promise(resolve=>server.close(resolve))
}
const result={scope:'Implementation production build, local API fixtures only; no real AWS',results,errors,requests,
  passed:results.filter(r=>r.passed).length,total:results.length}
await fs.writeFile(path.join(output,'browser.json'),JSON.stringify(result,null,2))
console.log(JSON.stringify({results,errors,passed:result.passed,total:result.total},null,2))

if (results.some(row => !row.passed) || errors.length) process.exitCode = 1
