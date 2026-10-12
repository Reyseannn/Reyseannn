const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();
const el=()=>({innerHTML:'',textContent:'',classList:{add(){},remove(){},toggle(){}},querySelectorAll:()=>[],open:false,showModal(){this.open=true},close(){this.open=false}});
const document={querySelector:s=>{if(!elements.has(s))elements.set(s,el());return elements.get(s)},querySelectorAll:()=>[],addEventListener(){},createElement:()=>el(),head:{append(){}}};
const context={document,location:{hash:'#houses'},window:{addEventListener(){},scrollTo(){}},navigator:{onLine:true},Intl,Date,Number,String,Math,JSON,Map,Promise,structuredClone,crypto:require('crypto').webcrypto,setTimeout:()=>0,clearTimeout(){},fetch:async()=>{throw Error('No API in static demo')}};
vm.createContext(context);
let code=fs.readFileSync('dist/app.js','utf8').replace(/init\(\);\s*$/,'');
vm.runInContext(code,context);
function run(s){return vm.runInContext(s,context)}
assert.equal(run('state.houses.length'),10);
run('renderHouses()');assert(elements.get('#main').innerHTML.includes('A place for every house.'));
for(const route of ['#house/H01','#house/H09','#payments','#reports','#archive','#settings']){context.location.hash=route;run('render()');assert(elements.get('#main').innerHTML.length>100)}
assert.equal(run("nextDue('2025-01-31',2)"),'2025-03-31');
run("state=freshState();user={name:'Owner',role:'owner'};applyDemo('tenant',{houseId:'H01',name:'Test tenant',contact:'09',moveIn:'2025-01-31',deposit:400000,advance:400000});applyDemo('bill',{houseId:'H01',period:'2025-02',due:'2025-02-28',mode:'meter',rent:400000,other:0,readings:{electricPrevious:100,electricCurrent:110,waterPrevious:20,waterCurrent:22}})");
assert.equal(run('billTotal(state.bills[0])'),439000);
run("user={name:'Mom',role:'cashier'};applyDemo('payment',{houseId:'H01',amount:500000,date:'2025-03-01',requestId:'test1'})");
assert.equal(run('state.tenants[0].credit'),61000);
assert.equal(run('state.payments[0].balanceAfter'),0);
assert(run("printContent('receipt','test1')").includes('Amount received'));
assert(run("printContent('statement','H01')").includes('not proof of payment'));
assert.throws(()=>run("applyDemo('rates',{electricRate:10,waterRate:20})"));
run("applyDemo('payment',{houseId:'H01',amount:500000,date:'2025-03-01',requestId:'test1'})");assert.equal(run('state.payments.length'),1);
run("user={name:'Owner',role:'owner'};applyDemo('bill',{houseId:'H01',period:'2025-03',due:'2025-03-31',mode:'manual',rent:400000,electric:25000,water:14000,other:0})");
assert.equal(run('state.bills[1].paid'),61000);
run("applyDemo('archive',{houseId:'H01',date:'2025-03-31',checklist:true,damage:0,notes:''});applyDemo('settle',{tenantId:state.tenants[0].id,applied:378000,refunded:422000,notes:'Settled'})");
assert.equal(run('tenantBalance(state.tenants[0])'),0);
console.log('Client checks passed: rendering all screens, meter calculations, credits, permissions, receipt content, retries, and settlement.');
