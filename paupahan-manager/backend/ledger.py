"""Rental ledger rules. Money is stored as integer centavos, never floats."""
from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import re
import uuid

class LedgerError(ValueError):
    pass

def amount(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0) or value > 100_000_000:
        raise LedgerError('Enter a valid amount in centavos.')
    return value

def text(value, limit=500, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise LedgerError('A required field is missing or too long.')
    return value.strip()

def valid_date(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError()
        date.fromisoformat(value)
        return value
    except (TypeError, ValueError):
        raise LedgerError('Enter a valid date.')

def next_due(move_in, months=1):
    d = date.fromisoformat(valid_date(move_in))
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1])).isoformat()

def fresh_state():
    return {'version': 0, 'settings': {'electricRate': 2500, 'waterRate': 7000, 'name': 'Paupahan Manager', 'address': 'Rental property'},
            'houses': [{'id': f'H{i:02}', 'label': f'House {i:02}', 'rent': 400000, 'tenantId': None} for i in range(1, 11)],
            'tenants': [], 'bills': [], 'payments': [], 'audit': []}

def total(b):
    return sum(b[k] for k in ('rent', 'electric', 'water', 'other'))

def tenant_bills(s, tid):
    return sorted((b for b in s['bills'] if b['tenantId'] == tid), key=lambda b: (b['due'], b['id']))

def balance(s, tid):
    return sum(total(b) - b['paid'] for b in tenant_bills(s, tid))

def get_record(records, identifier):
    found = next((r for r in records if r['id'] == identifier), None)
    if not found:
        raise LedgerError('Record not found. Refresh and try again.')
    return found

def reading(value):
    if isinstance(value, bool):
        raise LedgerError('Enter a valid meter reading.')
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or n > 10_000_000:
            raise InvalidOperation()
        return n
    except (InvalidOperation, ValueError, TypeError):
        raise LedgerError('Enter a valid meter reading.')

def calculate_bill(p, settings):
    mode = p.get('mode')
    if mode not in ('meter', 'manual'):
        raise LedgerError('Choose automatic or manual utilities.')
    charges = {k: amount(p.get(k, 0)) for k in ('rent', 'electric', 'water', 'other')}
    readings = {}
    if mode == 'meter':
        for utility in ('electric', 'water'):
            previous = reading(p.get('readings', {}).get(utility + 'Previous'))
            present = reading(p.get('readings', {}).get(utility + 'Current'))
            if present < previous:
                raise LedgerError('Present readings cannot be below previous readings.')
            charges[utility] = amount(int(((present - previous) * settings[utility + 'Rate']).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))
            readings[utility + 'Previous'] = float(previous)
            readings[utility + 'Current'] = float(present)
    return {**charges, 'mode': mode, 'readings': readings, 'notes': text(p.get('notes', ''))}

def allocate(s, tid, received):
    allocations, remaining = [], received
    for b in tenant_bills(s, tid):
        used = min(remaining, total(b) - b['paid'])
        if used:
            b['paid'] += used
            remaining -= used
            allocations.append({'billId': b['id'], 'amount': used, 'billSnapshot': deepcopy(b)})
    return allocations, remaining

def apply_action(original, action, p, user):
    """Return a new state; callers save it atomically using its previous version."""
    if user.get('role') not in ('owner', 'cashier', 'viewer'):
        raise LedgerError('This account has no rental workspace access.')
    if user['role'] == 'viewer' or (action != 'payment' and user['role'] != 'owner'):
        raise PermissionError('Only the owner can change this record.')
    if not isinstance(p, dict):
        raise LedgerError('Invalid request.')
    s = deepcopy(original)
    now = datetime.now(timezone.utc).isoformat()
    h = next((h for h in s['houses'] if h['id'] == p.get('houseId')), None)
    t = next((t for t in s['tenants'] if h and t['id'] == h['tenantId'] and not t['archived']), None)
    payment = None
    if action == 'house':
        label = text(p.get('label'), 60, True)
        if any(h['label'].lower() == label.lower() for h in s['houses']):
            raise LedgerError('That house name already exists.')
        s['houses'].append({'id': str(uuid.uuid4()), 'label': label, 'rent': amount(p.get('rent')), 'tenantId': None})
    elif action == 'tenant':
        if not h or t:
            raise LedgerError('Choose a vacant house.')
        policy = p.get('advancePolicy', 'unconfirmed')
        if policy not in ('unconfirmed', 'first-month', 'last-month'):
            raise LedgerError('Choose an advance policy.')
        t = {'id': str(uuid.uuid4()), 'houseId': h['id'], 'name': text(p.get('name'), 100, True), 'contact': text(p.get('contact'), 30, True),
             'moveIn': valid_date(p.get('moveIn')), 'deposit': amount(p.get('deposit')), 'advance': amount(p.get('advance')), 'advancePolicy': policy,
             'credit': 0, 'documents': [], 'archived': False}
        if t['moveIn'] > datetime.now(timezone(timedelta(hours=8))).date().isoformat():
            raise LedgerError('Move-in date cannot be in the future.')
        s['tenants'].append(t)
        h['tenantId'] = t['id']
    elif action == 'bill':
        if not t:
            raise LedgerError('Assign a tenant first.')
        period = p.get('period', '')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
            raise LedgerError('Enter a valid billing month.')
        if any(b['period'] == period for b in tenant_bills(s, t['id'])):
            raise LedgerError('A bill for this month already exists.')
        due = valid_date(p.get('due'))
        if due < t['moveIn']:
            raise LedgerError('The due date cannot be before move-in.')
        b = {**calculate_bill(p, s['settings']), 'id': str(uuid.uuid4()), 'tenantId': t['id'], 'houseId': h['id'], 'period': period, 'due': due,
             'electricRate': s['settings']['electricRate'], 'waterRate': s['settings']['waterRate'], 'paid': 0, 'createdAt': now}
        s['bills'].append(b)
        used = min(t['credit'], total(b))
        if used:
            b['paid'] += used
            t['credit'] -= used
            s['audit'].append({'action': 'credit_applied', 'date': now, 'by': user['name'], 'billId': b['id'], 'amount': used})
    elif action == 'payment':
        request_id = text(p.get('requestId'), 80, True)
        existing = next((x for x in s['payments'] if x['id'] == request_id), None)
        if existing:
            return original, existing
        if not t:
            raise LedgerError('No active tenant.')
        received = amount(p.get('amount'), True)
        payment_date = valid_date(p.get('date'))
        if payment_date < t['moveIn'] or payment_date > datetime.now(timezone(timedelta(hours=8))).date().isoformat():
            raise LedgerError('Payment date must be between move-in and today.')
        allocations, remaining = allocate(s, t['id'], received)
        t['credit'] += remaining
        payment = {'id': request_id, 'receipt': f"PM-{payment_date[:4]}-{len(s['payments']) + 1:05}", 'tenantId': t['id'], 'houseId': h['id'],
                   'tenantName': t['name'], 'houseLabel': h['label'], 'amount': received, 'date': payment_date, 'recordedAt': now,
                   'recordedBy': user['name'], 'notes': text(p.get('notes', '')), 'allocations': allocations, 'credit': remaining, 'balanceAfter': balance(s, t['id']), 'voided': False}
        s['payments'].append(payment)
    elif action == 'rates':
        if p.get('houseId') and not h:
            raise LedgerError('House not found.')
        if h:
            h['rent'] = amount(p.get('rent'))
        for key in ('electricRate', 'waterRate'):
            s['settings'][key] = amount(p.get(key))
    elif action == 'settings':
        s['settings']['name'] = text(p.get('name'), 100, True)
        s['settings']['address'] = text(p.get('address'), 200, True)
    elif action == 'bill_correction':
        b = get_record(s['bills'], p.get('billId'))
        reason = text(p.get('reason'), 500, True)
        previous = deepcopy(b)
        corrected = calculate_bill(p, {'electricRate': b['electricRate'], 'waterRate': b['waterRate']})
        if total(corrected) < b['paid']:
            raise LedgerError('Corrected total is below the payments already applied. Review or reverse the affected payments first.')
        b.update(corrected)
        s['audit'].append({'action': 'bill_previous_version', 'date': now, 'by': user['name'], 'reason': reason, 'bill': previous})
    elif action == 'void':
        pay = get_record(s['payments'], p.get('paymentId'))
        if pay['voided']:
            raise LedgerError('This payment has already been voided.')
        reason = text(p.get('reason'), 500, True)
        tenant = get_record(s['tenants'], pay['tenantId'])
        if tenant['archived'] or pay['credit'] > tenant['credit']:
            raise LedgerError('This payment has funds in a later bill or settlement. Review the ledger before reversing it.')
        for a in pay['allocations']:
            b = get_record(s['bills'], a['billId'])
            b['paid'] -= a['amount']
        tenant['credit'] -= pay['credit']
        pay.update({'voided': True, 'voidReason': reason, 'voidedAt': now, 'voidedBy': user['name']})
    elif action == 'archive':
        if not t:
            raise LedgerError('No active tenant.')
        if p.get('checklist') is not True:
            raise LedgerError('Complete the settlement review first.')
        move_out = valid_date(p.get('date'))
        if move_out < t['moveIn']:
            raise LedgerError('Move-out cannot be before move-in.')
        t.update({'archived': True, 'moveOut': move_out, 'settlement': {'outstanding': balance(s, t['id']), 'deposit': t['deposit'], 'advance': t['advance'],
                    'credit': t['credit'], 'damage': amount(p.get('damage', 0)), 'notes': text(p.get('notes', '')), 'status': 'pending', 'applied': 0, 'refunded': 0}})
        h['tenantId'] = None
    elif action == 'settle':
        tenant = get_record(s['tenants'], p.get('tenantId'))
        if not tenant['archived']:
            raise LedgerError('Only archived tenants can be settled.')
        settlement = tenant['settlement']
        applied, refunded = amount(p.get('applied')), amount(p.get('refunded'))
        held = settlement['deposit'] + settlement['advance'] + settlement['credit']
        if applied < settlement['applied'] or refunded < settlement['refunded']:
            raise LedgerError('Recorded settlement totals cannot be reduced. Request a ledger review.')
        if applied > settlement['outstanding'] + settlement['damage'] or applied + refunded > held:
            raise LedgerError('Amounts exceed recorded dues or held funds.')
        increment = min(applied, settlement['outstanding']) - min(settlement['applied'], settlement['outstanding'])
        if increment:
            allocate(s, tenant['id'], increment)
        settlement.update({'applied': applied, 'refunded': refunded, 'notes': text(p.get('notes'), 500, True), 'status': 'settled' if applied + refunded == held else 'pending'})
    elif action == 'document_add':
        tenant = get_record(s['tenants'], p.get('tenantId'))
        document = {'id': text(p.get('id'), 80, True), 'name': text(p.get('name'), 150, True), 'path': text(p.get('path'), 250, True), 'createdAt': now}
        tenant['documents'].append(document)
    elif action == 'document_delete':
        tenant = get_record(s['tenants'], p.get('tenantId'))
        document = get_record(tenant['documents'], p.get('documentId'))
        tenant['documents'].remove(document)
    else:
        raise LedgerError('Unknown action.')
    # Audit corrections but do not duplicate IDs or personal form values in audit details.
    s['audit'].append({'action': action, 'date': now, 'by': user['name'], 'recordId': p.get('houseId') or p.get('tenantId') or p.get('billId') or p.get('paymentId')})
    s['version'] += 1
    return s, payment
