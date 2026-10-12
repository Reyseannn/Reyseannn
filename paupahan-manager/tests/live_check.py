"""Temporary-account live QA. Supply credentials on stdin, never as arguments."""
import json,sys,uuid,urllib.request,urllib.error,http.cookiejar
if sys.stdin.isatty():
 import termios
 attrs=termios.tcgetattr(sys.stdin.fileno());attrs[3]&=~termios.ECHO;termios.tcsetattr(sys.stdin.fileno(),termios.TCSANOW,attrs)
p=json.loads(sys.stdin.readline())
base=p['url'].rstrip('/')
results=[]
def client():
 return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def request(c,path,body=None,method=None,raw=None,content_type='application/json'):
 data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
 req=urllib.request.Request(base+'/api/'+path,data=data,method=method or ('POST' if data is not None else 'GET'),headers={'Content-Type':content_type})
 try:
  with c.open(req,timeout=25) as r:return r.status,json.loads(r.read())
 except urllib.error.HTTPError as e:return e.code,json.loads(e.read())
def ok(condition,label):
 if not condition:raise AssertionError(label)
 results.append(label)
owner,cashier,unapproved=client(),client(),client()
accounts={a['role']:a for a in p['accounts']}
for role,c in [('owner',owner),('cashier',cashier)]:
 code,r=request(c,'login',{'email':accounts[role]['email'],'password':p['password']})
 if code!=200:raise AssertionError(f'{role} login failed: HTTP {code} {r}')
 ok(r['user']['role']==role,role+' sign-in')
 if role=='owner':initial=r['state']
code,r=request(unapproved,'login',{'email':accounts['unapproved']['email'],'password':p['password']})
ok(code==403,'unapproved account blocked')
ok(request(client(),'state')[0]==401,'anonymous ledger blocked')
state=initial
def action(c,action,payload,expected=200):
 global state
 code,r=request(c,'action',{'action':action,'payload':payload,'version':state['version']})
 if code!=expected:raise AssertionError(f'{action}: HTTP {code} {r}')
 if code==200:state=r['state']
 return r
label='QA-'+uuid.uuid4().hex[:8]
action(owner,'house',{'label':label,'rent':400000});house=next(h for h in state['houses'] if h['label']==label)
action(owner,'tenant',{'houseId':house['id'],'name':'QA Tenant','contact':'QA only','moveIn':'2025-01-01','deposit':400000,'advance':400000,'advancePolicy':'unconfirmed'})
tenant=next(t for t in state['tenants'] if t['houseId']==house['id'])
action(owner,'bill',{'houseId':house['id'],'period':'2025-02','due':'2025-02-01','mode':'meter','rent':400000,'other':0,'readings':{'electricPrevious':100,'electricCurrent':110,'waterPrevious':20,'waterCurrent':22}})
bill=next(b for b in state['bills'] if b['tenantId']==tenant['id'])
ok(bill['electric']==25000 and bill['water']==14000,'live meter calculation')
request_id=uuid.uuid4().hex
payment=action(cashier,'payment',{'houseId':house['id'],'amount':100000,'date':'2025-02-02','requestId':request_id})['payment']
ok(payment['balanceAfter']==339000,'live partial payment and balance')
version=state['version'];second=action(cashier,'payment',{'houseId':house['id'],'amount':100000,'date':'2025-02-02','requestId':request_id})
ok(state['version']==version and second['payment']['receipt']==payment['receipt'],'live retry returns same receipt')
action(cashier,'rates',{'electricRate':3000,'waterRate':8000},403)
ok(True,'cashier cannot change rates')
# Multipart document upload with harmless PDF fixture, no real IDs.
boundary='QA'+uuid.uuid4().hex
parts=(f'--{boundary}\r\nContent-Disposition: form-data; name="tenantId"\r\n\r\n{tenant["id"]}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="qa-document.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()+b'%PDF-1.4\n%Harmless temporary validation fixture\n'+f'\r\n--{boundary}--\r\n'.encode()
code,r=request(owner,'documents',method='POST',raw=parts,content_type='multipart/form-data; boundary='+boundary)
if code!=200:raise AssertionError(f'Upload HTTP {code}: {r}')
state=request(owner,'state')[1]['state'];document=next(t for t in state['tenants'] if t['id']==tenant['id'])['documents'][0]
code,r=request(cashier,'documents/'+document['id']);ok(code==200 and r['url'].startswith('https://'),'family-only signed document link')
with urllib.request.urlopen(r['url'],timeout=25) as response:ok(response.read().startswith(b'%PDF-'),'signed document opens')
code,r=request(client(),'documents/'+document['id']);ok(code==401,'anonymous private document blocked')
code,r=request(owner,'documents/'+document['id'],method='DELETE');ok(code==200,'owner can remove private file');state=request(owner,'state')[1]['state']
action(owner,'void',{'paymentId':payment['id'],'reason':'Temporary QA reversal'})
ok(next(b for b in state['bills'] if b['id']==bill['id'])['paid']==0,'correction reverses balance')
action(owner,'rates',{'electricRate':3000,'waterRate':8000})
ok(next(b for b in state['bills'] if b['id']==bill['id'])['electricRate']==2500,'saved bill keeps original rate')
action(owner,'archive',{'houseId':house['id'],'date':'2025-03-01','checklist':True,'damage':0,'notes':'Temporary QA move-out'})
action(owner,'settle',{'tenantId':tenant['id'],'applied':439000,'refunded':361000,'notes':'Temporary QA settlement'})
ok(next(b for b in state['bills'] if b['id']==bill['id'])['paid']==439000,'held funds settle unpaid bills')
request(owner,'logout',{},'POST');request(cashier,'logout',{},'POST')
print(json.dumps({'checks':results,'final_version':state['version'],'initial_state':initial,'qa_house':house['id']}))
