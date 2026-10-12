# Paupahan Manager

The live app is at https://paupahan-manager-reysean.onrender.com.
The owner-private Sites version is a separate fictional-data demo.

## First use

Create a family login using your privately approved Owner email. Choose your own
password (at least 10 characters), confirm the email, return to the site and sign in.
In Settings > Family access, approve a family member as Cashier or Viewer before
they create their login. Unapproved accounts cannot read your workspace.

Your database starts with 10 empty houses and no fabricated payment balances.
Set each house's actual rent, assign its tenant, and enter current meter readings
when creating the first monthly bill. Shared defaults are ₱25/kWh electricity and
₱70/m³ water. Stored bills keep their original rates.

## Language and files

HTML, CSS, JavaScript, and a Python standard-library backend. No React.
- dist/index.html, styles.css, app.js: responsive interface and print formats.
- api/index.py: authenticated Python API and private uploads.
- backend/ledger.py: billing and permission checks.
- backend/schema.sql: database setup, RLS and payment operations.
- server.py: local / hosted entrypoint.
- render.yaml: free Render hosting configuration.
- tests/: calculation and live-system checks.

Source code on GitHub is generic. Real property details, tenants, approved emails,
payments, and uploaded documents belong in the private Supabase database/storage.
The app is in its own branch and folder in the account's profile repository.

## Local run

Run `python server.py` and open http://127.0.0.1:8000. No pip install needed.
For live mode set SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY and COOKIE_SECRET
in the environment. The existing Render service is already configured.
Do not paste credentials into chat or commit them. The app has no database
administrator key: data access uses signed-in users, RLS, and bounded RPCs.

## Billing and collections

Rent, utility charges, and other agreed charges are integer centavos internally.
Automatic utilities use (present minus previous) readings times the stored rate.
Manual utility amounts are available. Due dates follow the move-in day and handle
short months without permanently shifting the schedule.

Partial payments settle oldest unpaid bills first. Any excess becomes future
payment credit. Retries use the same request ID and return the same receipt.
There are no late fees. Unpaid bills appear once on the billing statement.
Deposits and move-in advances stay separate until you review their intended use.
The first/last-month advance choice records the policy; no bill is paid silently.

The Cashier can confirm new payments, view, print, and export. Only the Owner can
change rates, create/correct bills, reverse payments, manage tenants/documents,
and settle move-outs. PostgreSQL enforces the Cashier payment boundary in addition
to the Python checks.

Corrections preserve original receipts or bill snapshots in the audit trail.
Archive a tenant through the move-out checklist; the house becomes vacant while
the old tenant keeps a separate history. Settlement applies held funds and records
refunds without counting the same money as newly collected income.

## Reports and printing

Monthly CSV payment reports open in Excel or Google Sheets. Voided receipts are
labeled and excluded from totals. These are cash collection reports, not profit
reports. Initial held-fund amounts remain separate from bill-payment collections.
Receipts are saved as records with allocation snapshots and can be reprinted.
Use the print dialog to save a PDF. Select 58 mm for the VOZY P50 or a formal
full-page layout. Connect through USB, select the correct driver/paper size, and
disable browser headers and footers. Physical output must be checked on your laptop.
These are internal records; the system makes no tax-receipt certification claim.

## Private documents and limits

Owner uploads accept PDF/JPG/PNG up to 3 MB and check file signatures. The storage
bucket is private, and document links expire after 60 seconds. Approved family
accounts can view documents; only the Owner may upload/remove them.

Sign-in sessions last one hour. Sign in again afterward. The web app does not
queue payments offline; note them on paper and enter them after reconnecting.
Free Render can sleep when idle, so its first request may take time. Supabase
keeps the records/files when the app sleeps or redeploys. Free hosting has limits.
Keep separate protected backups of original documents and monthly reports.
Document retention has no automatic deletion timer; choose that period before
enabling automated deletion.

## Verification

Run `python -m unittest discover -s tests -v`, `node --check dist/app.js`,
and `node tests/test_client.cjs`. The live verification script accepts temporary
credentials on stdin and never commits them.
