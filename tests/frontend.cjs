// Run: node tests/frontend.cjs. Executes production functions, without a DOM package.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const page = fs.readFileSync(path.join(__dirname, '..', 'overlay.html'), 'utf8');
for (const script of page.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
  new vm.Script(script[1], {filename:'overlay.html'});
}
const segment = (start, end) => page.slice(page.indexOf(start), page.indexOf(end, page.indexOf(start)));
let box = {value:'Keep this note',disabled:false,blur(){}}, error = {textContent:'',hidden:true};
const ctx = vm.createContext({console, $: id => id === 'note-error' ? error : box,
  currentPlace: () => ({game:'oot',scene:'KOKIRI_FOREST',room:0}), tick:async()=>{},
  esc:String, keepGame:()=>true, entMatch:()=>true, showPending:true});
vm.runInContext('let notePosting = false;'+segment('function noteError(', 'function notePlace('),ctx);
vm.runInContext(segment('async function postNote(', 'function renderNotes('),ctx);
vm.runInContext(segment('function renderEntrances(', '/* The hints panel:'),ctx);
(async () => {
  ctx.fetch = async () => {throw new Error('offline')};
  await vm.runInContext('postNote(true)',ctx);
  assert.equal(box.value,'Keep this note'); assert.equal(box.disabled,false); assert.equal(error.hidden,false);
  ctx.fetch = async () => ({ok:true,json:async()=>({ok:false,error:'Disk full'})});
  await vm.runInContext('postNote(true)',ctx);
  assert.equal(box.value,'Keep this note'); assert.equal(error.textContent,'Disk full');
  let resolve, requests = 0;
  ctx.fetch = () => {requests++; return new Promise(r=>{resolve=r})};
  const pending = vm.runInContext('postNote(true)',ctx);
  await vm.runInContext('postNote(true)',ctx);
  assert.equal(requests,1); assert.equal(box.value,'Keep this note');
  resolve({ok:true,json:async()=>({ok:true})}); await pending;
  assert.equal(box.value,''); assert.equal(box.disabled,false); assert.equal(error.hidden,true);
  const entries=Array.from({length:41},(_,i)=>({game:'oot',src:i,from_area:String(i),to_area:String(i+100),sure:true}));
  ctx.en={all:entries,found:entries};
  assert.equal((vm.runInContext('renderEntrances(en)',ctx).match(/class="todo"/g)||[]).length,0);
  console.log('PASS: network and disk failures retain text; successful save clears it; duplicate submit blocked; 41 visits remain visited');
})().catch(e=>{console.error(e);process.exitCode=1});
