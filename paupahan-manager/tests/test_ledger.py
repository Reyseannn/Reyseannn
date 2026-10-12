import unittest
from copy import deepcopy
from backend.ledger import fresh_state, apply_action, balance, total, next_due, LedgerError
OWNER={'name':'Owner','role':'owner'}
CASHIER={'name':'Mom','role':'cashier'}

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.s=fresh_state()
        self.s,_=apply_action(self.s,'tenant',{'houseId':'H01','name':'Test tenant','contact':'09000000000','moveIn':'2025-01-31','deposit':400000,'advance':400000},OWNER)
        self.t=self.s['tenants'][0]['id']
    def bill(self,period='2025-02',due='2025-02-28'):
        self.s,_=apply_action(self.s,'bill',{'houseId':'H01','period':period,'due':due,'mode':'meter','rent':400000,'other':0,'readings':{'electricPrevious':100,'electricCurrent':110,'waterPrevious':20,'waterCurrent':22}},OWNER)
    def payment(self,n,identifier='p1'):
        self.s,p=apply_action(self.s,'payment',{'houseId':'H01','amount':n,'date':'2025-03-01','requestId':identifier},CASHIER)
        return p
    def test_due_dates_anchor_to_move_in(self):
        self.assertEqual(next_due('2025-01-31'), '2025-02-28')
        self.assertEqual(next_due('2025-01-31',2), '2025-03-31')
        self.assertEqual(next_due('2024-01-31'), '2024-02-29')
    def test_meter_formula_and_rate_snapshots(self):
        self.bill();b=deepcopy(self.s['bills'][0])
        self.assertEqual(b['electric'],25000);self.assertEqual(b['water'],14000)
        self.s,_=apply_action(self.s,'rates',{'electricRate':3000,'waterRate':8000},OWNER)
        self.assertEqual(self.s['bills'][0],b)
    def test_partial_and_oldest_first(self):
        self.bill();self.bill('2025-03','2025-03-31')
        self.payment(450000)
        self.assertEqual(self.s['bills'][0]['paid'],439000)
        self.assertEqual(self.s['bills'][1]['paid'],11000)
        self.assertEqual(balance(self.s,self.t),428000)
    def test_credit_applies_once(self):
        self.bill();self.payment(500000)
        self.assertEqual(self.s['tenants'][0]['credit'],61000)
        self.bill('2025-03','2025-03-31')
        self.assertEqual(self.s['tenants'][0]['credit'],0)
        self.assertEqual(self.s['bills'][1]['paid'],61000)
    def test_idempotent_payment(self):
        self.bill();p=self.payment(200000)
        version=self.s['version'];p2=self.payment(200000)
        self.assertEqual(p,p2);self.assertEqual(len(self.s['payments']),1)
        self.assertEqual(self.s['version'],version)
    def test_cashier_permissions(self):
        for action,p in [('rates',{'electricRate':1,'waterRate':1}),('house',{'label':'new','rent':1}),('archive',{'houseId':'H01'}),('bill',{'houseId':'H01'}),('void',{'paymentId':'x'})]:
            with self.subTest(action=action),self.assertRaises(PermissionError):
                apply_action(self.s,action,p,CASHIER)
    def test_invalid_reading_does_not_change_state(self):
        previous=deepcopy(self.s)
        with self.assertRaises(LedgerError):
            apply_action(self.s,'bill',{'houseId':'H01','period':'2025-02','due':'2025-02-28','mode':'meter','rent':400000,'readings':{'electricPrevious':100,'electricCurrent':99,'waterPrevious':0,'waterCurrent':1}},OWNER)
        self.assertEqual(previous,self.s)
    def test_void_preserves_receipt_and_reverses_balance(self):
        self.bill();p=self.payment(200000)
        self.s,_=apply_action(self.s,'void',{'paymentId':p['id'],'reason':'Wrong amount'},OWNER)
        self.assertEqual(balance(self.s,self.t),439000)
        self.assertTrue(self.s['payments'][0]['voided'])
        self.assertEqual(self.s['payments'][0]['receipt'],p['receipt'])
    def test_cannot_void_credit_already_used(self):
        self.bill();p=self.payment(500000);self.bill('2025-03','2025-03-31')
        with self.assertRaises(LedgerError):apply_action(self.s,'void',{'paymentId':p['id'],'reason':'Mistake'},OWNER)
    def test_archive_new_tenant_keeps_histories_separate(self):
        self.bill();self.payment(100000)
        self.s,_=apply_action(self.s,'archive',{'houseId':'H01','date':'2025-03-01','checklist':True,'damage':0},OWNER)
        old=self.t
        self.s,_=apply_action(self.s,'tenant',{'houseId':'H01','name':'New tenant','contact':'09000000000','moveIn':'2025-03-02','deposit':0,'advance':0},OWNER)
        new=self.s['houses'][0]['tenantId']
        self.assertNotEqual(old,new);self.assertEqual(balance(self.s,new),0)
        self.assertEqual(balance(self.s,old),339000)
    def test_settlement_no_duplicate_income(self):
        self.bill()
        self.s,_=apply_action(self.s,'archive',{'houseId':'H01','date':'2025-03-01','checklist':True,'damage':0},OWNER)
        self.s,_=apply_action(self.s,'settle',{'tenantId':self.t,'applied':439000,'refunded':361000,'notes':'All funds settled'},OWNER)
        self.assertEqual(balance(self.s,self.t),0)
        self.assertEqual(self.s['tenants'][0]['settlement']['status'],'settled')
        self.assertEqual(len(self.s['payments']),0)
    def test_duplicate_month_rejected(self):
        self.bill()
        with self.assertRaises(LedgerError):self.bill()
    def test_manual_utilities(self):
        self.s,_=apply_action(self.s,'bill',{'houseId':'H01','period':'2025-02','due':'2025-02-28','mode':'manual','rent':400000,'electric':50000,'water':15000,'other':10000},OWNER)
        self.assertEqual(total(self.s['bills'][0]),475000)
    def test_correction_keeps_old_readings_in_audit(self):
        self.bill();b=self.s['bills'][0]
        self.s,_=apply_action(self.s,'bill_correction',{'billId':b['id'],'mode':'meter','rent':400000,'other':0,'reason':'Typo in present meter reading','readings':{'electricPrevious':100,'electricCurrent':112,'waterPrevious':20,'waterCurrent':22}},OWNER)
        self.assertEqual(self.s['bills'][0]['electric'],30000)
        self.assertEqual(next(a for a in self.s['audit'] if a['action']=='bill_previous_version')['bill']['readings']['electricCurrent'],110)
    def test_nan_and_negative_rejected(self):
        for n in (-1,float('nan'),1.5,True):
            with self.assertRaises(LedgerError):self.payment(n)

if __name__=='__main__':unittest.main()
