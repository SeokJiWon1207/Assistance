const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const core=require('../backend/static/frontend_core.js');

async function harness(){
  const elements=new Map();
  function el(id){if(!elements.has(id)) elements.set(id,{hidden:false,disabled:false,textContent:'',checked:false,
    listeners:{},attributes:{},addEventListener(type,fn){this.listeners[type]=fn;},
    setAttribute(k,v){this.attributes[k]=v;},querySelector(){return this;},querySelectorAll(){return[];},focus(){}});return elements.get(id);}
  const state={currentUser:{uid:'a'},conversations:[{id:'one',title:'첫 대화',messages:[]},{id:'two',title:'다른 대화',messages:[]}],selectedConversationIds:new Set(),conversationSelectionMode:false};
  let calls=0;
  const context=vm.createContext({document:{querySelector:s=>el(s),activeElement:el('opener')},addEventListener(){}});
  const modules={
    './runtime.js':{state,els:{conversationManageBtn:el('#conversationManageBtn'),conversationSearch:el('#conversationSearch')},searchConversations:core.searchConversations},
    './workspace.js':{renderConversationList(){},bulkDeleteConversations:async()=>{calls++;return{deletedCount:2};},showToast(){}},
    './dialog_focus.js':{openDialogFocus(){},closeDialogFocus(){}},
  };
  const mod=new vm.SourceTextModule(fs.readFileSync(path.join(__dirname,'../backend/static/modules/conversation_bulk.js'),'utf8'),{context});
  await mod.link(spec=>{const exports=modules[spec.replace(/\?v=.*$/,'')];return new vm.SyntheticModule(Object.keys(exports),function(){for(const[k,v]of Object.entries(exports))this.setExport(k,v);},{context});});
  await mod.evaluate();mod.namespace.initConversationBulkActions();
  return{state,el,api:mod.namespace,calls:()=>calls};
}

test('management selects loaded matching records without deleting until confirmed',async()=>{
  const h=await harness();h.el('#conversationManageBtn').listeners.click();
  assert.equal(h.state.conversationSelectionMode,true);
  h.el('#conversationSearch').value='첫';h.el('#conversationSelectLoaded').checked=true;
  h.el('#conversationSelectLoaded').listeners.change();
  assert.deepEqual([...h.state.selectedConversationIds],['one']);
  h.el('#conversationDeleteSelected').listeners.click();
  assert.equal(h.el('#conversationDeleteModal').hidden,false);
  assert.match(h.el('#conversationDeleteTitle').textContent,/1/);
  assert.equal(h.calls(),0);
  h.el('#conversationDeleteCancel').listeners.click();
  assert.equal(h.el('#conversationDeleteModal').hidden,true);assert.equal(h.calls(),0);
});

test('all confirmation makes one delete request and blocks duplicate confirmation',async()=>{
  const h=await harness();h.el('#conversationManageBtn').listeners.click();h.el('#conversationDeleteAll').listeners.click();
  assert.match(h.el('#conversationDeleteDescription').textContent,/불러오지 않은/);
  assert.match(h.el('#conversationDeleteDescription').textContent,/복구할 수 없/);
  await h.el('#conversationDeleteConfirm').listeners.click();
  assert.equal(h.calls(),1);assert.equal(h.el('#conversationDeleteModal').hidden,true);
});

test('confirmation cannot send two concurrent deletion requests',async()=>{
  const h=await harness();h.el('#conversationManageBtn').listeners.click();h.el('#conversationDeleteAll').listeners.click();
  const first=h.el('#conversationDeleteConfirm').listeners.click();
  const second=h.el('#conversationDeleteConfirm').listeners.click();
  await Promise.all([first,second]);assert.equal(h.calls(),1);
});

test('selection outside a search remains explicit and the confirmation names selected records',async()=>{
  const h=await harness();h.el('#conversationManageBtn').listeners.click();
  h.state.selectedConversationIds.add('two');h.el('#conversationSearch').value='첫';
  h.el('#conversationSelectLoaded').checked=true;h.el('#conversationSelectLoaded').listeners.change();
  assert.match(h.el('#conversationSelectedCount').textContent,/2개 선택.*검색 밖 1개/);
  h.el('#conversationDeleteSelected').listeners.click();
  assert.match(h.el('#conversationDeleteDescription').textContent,/첫 대화.*다른 대화/);
});
