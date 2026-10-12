-- One family workspace, authenticated membership and private files.
create schema if not exists rental_private;
revoke all on schema rental_private from public;
grant usage on schema rental_private to authenticated;

create table public.rental_members (
 user_id uuid primary key references auth.users(id) on delete cascade,
 name text not null check(length(name) between 1 and 100),
 role text not null check(role in ('owner','cashier','viewer')),
 active boolean not null default true
);
alter table public.rental_members enable row level security;
revoke all on public.rental_members from anon, authenticated;
grant select on public.rental_members to authenticated;
create policy rental_member_own_role on public.rental_members for select to authenticated
 using(user_id=(select auth.uid()));

create table public.rental_access_emails (
 email text primary key check(email=lower(email)),
 name text not null check(length(name) between 1 and 100),
 role text not null check(role in ('owner','cashier','viewer')),
 active boolean not null default true
);
alter table public.rental_access_emails enable row level security;
revoke all on public.rental_access_emails from anon, authenticated;
grant select, insert, update on public.rental_access_emails to authenticated;

create table public.rental_workspace (
 id integer primary key check(id=1),version bigint not null default 0,
 data jsonb not null,updated_at timestamptz not null default now()
);
alter table public.rental_workspace enable row level security;
revoke all on public.rental_workspace from anon, authenticated;
grant select on public.rental_workspace to authenticated;

-- Internal authorization lookup; no user-editable metadata is used.
create function rental_private.rental_role() returns text
 language sql stable security definer set search_path='' as $$
 select role from public.rental_members where user_id=auth.uid() and active;
$$;
revoke all on function rental_private.rental_role() from public,anon;
grant execute on function rental_private.rental_role() to authenticated;
create policy rental_family_reads on public.rental_workspace for select to authenticated
 using ((select rental_private.rental_role()) is not null);
create policy rental_owner_access_read on public.rental_access_emails for select to authenticated
 using ((select rental_private.rental_role())='owner');
create policy rental_owner_access_insert on public.rental_access_emails for insert to authenticated
 with check ((select rental_private.rental_role())='owner');
create policy rental_owner_access_update on public.rental_access_emails for update to authenticated
 using ((select rental_private.rental_role())='owner') with check ((select rental_private.rental_role())='owner');

-- Only trusted, preapproved emails become family members at sign-up.
create function rental_private.attach_rental_member() returns trigger
 language plpgsql security definer set search_path='' as $$
begin
 insert into public.rental_members(user_id,name,role,active)
 select new.id,a.name,a.role,a.active from public.rental_access_emails a
 where a.email=lower(new.email)
 on conflict(user_id) do update set name=excluded.name,role=excluded.role,active=excluded.active;
 return new;
end;
$$;
revoke all on function rental_private.attach_rental_member() from public,anon,authenticated;
create trigger rental_attach_new_auth_user after insert on auth.users
 for each row execute function rental_private.attach_rental_member();

create function rental_private.sync_rental_access() returns trigger
 language plpgsql security definer set search_path='' as $$
begin
 -- Called by the roster table's owner-only RLS-controlled writes.
 insert into public.rental_members(user_id,name,role,active)
 select u.id,new.name,new.role,new.active from auth.users u where lower(u.email)=new.email
 on conflict(user_id) do update set name=excluded.name,role=excluded.role,active=excluded.active;
 return new;
end;
$$;
revoke all on function rental_private.sync_rental_access() from public,anon,authenticated;
create trigger rental_sync_email_access after insert or update on public.rental_access_emails
 for each row execute function rental_private.sync_rental_access();

-- This internal privileged write is needed because clients have SELECT only.
-- It explicitly verifies authenticated Owner access and uses a version lease.
create function rental_private.save_rental_workspace(expected_version bigint,new_data jsonb)
returns boolean language plpgsql security definer set search_path='' as $$
declare changed integer;
begin
 if auth.uid() is null or rental_private.rental_role() is distinct from 'owner' then
  raise exception 'Owner access required' using errcode='42501';
 end if;
 if jsonb_typeof(new_data) is distinct from 'object' then raise exception 'Invalid ledger';end if;
 update public.rental_workspace set data=new_data,version=version+1,updated_at=now()
 where id=1 and version=expected_version;
 get diagnostics changed=row_count;
 return changed=1;
end;
$$;
revoke all on function rental_private.save_rental_workspace(bigint,jsonb) from public,anon;
grant execute on function rental_private.save_rental_workspace(bigint,jsonb) to authenticated;
create function public.rental_save_workspace(expected_version bigint,new_data jsonb)
returns boolean language sql security invoker set search_path='' as $$
 select rental_private.save_rental_workspace(expected_version,new_data);
$$;
revoke all on function public.rental_save_workspace(bigint,jsonb) from public,anon;
grant execute on function public.rental_save_workspace(bigint,jsonb) to authenticated;

-- Cashiers get this bounded payment operation, not arbitrary ledger writes.
create function rental_private.record_rental_payment(expected_version bigint,payload jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare
 ledger jsonb;v bigint;h jsonb;t jsonb;hidx integer;tidx integer;item record;b jsonb;
 requested bigint;remaining bigint;used bigint;bill_sum bigint;outstanding bigint;
 payment jsonb;allocations jsonb:='[]'::jsonb;existing jsonb;name text;role text;
 request_id text;payment_date date;tid text;now_text text;actor_id uuid;
begin
 actor_id:=auth.uid();
 if actor_id is null then raise exception 'Sign in first' using errcode='42501';end if;
 select m.name,m.role into name,role from public.rental_members m where m.user_id=actor_id and m.active;
 if role is null or role not in ('owner','cashier') then raise exception 'Payment access required' using errcode='42501';end if;
 request_id:=payload->>'requestId';
 if request_id is null or length(request_id) not between 1 and 80 then raise exception 'Invalid request ID';end if;
 if jsonb_typeof(payload->'amount') is distinct from 'number' or (payload->>'amount') !~ '^[0-9]+$' then raise exception 'Invalid amount';end if;
 requested:=(payload->>'amount')::bigint;
 if requested not between 1 and 100000000 then raise exception 'Invalid amount';end if;
 if length(coalesce(payload->>'notes',''))>500 then raise exception 'Notes too long';end if;
 payment_date:=(payload->>'date')::date;
 if payment_date is null then raise exception 'Payment date required';end if;
 select w.data,w.version into ledger,v from public.rental_workspace w where w.id=1 for update;
 select p into existing from jsonb_array_elements(ledger->'payments') p where p->>'id'=request_id;
 if existing is not null then return jsonb_build_object('state',ledger,'payment',existing);end if;
 if v<>expected_version then raise exception 'Ledger changed; refresh and try again' using errcode='40001';end if;
 select x.value,(x.ordinality-1)::integer into h,hidx from jsonb_array_elements(ledger->'houses') with ordinality x where x.value->>'id'=payload->>'houseId';
 if h is null or h->>'tenantId' is null then raise exception 'No active tenant';end if;
 tid:=h->>'tenantId';
 select x.value,(x.ordinality-1)::integer into t,tidx from jsonb_array_elements(ledger->'tenants') with ordinality x where x.value->>'id'=tid and (x.value->>'archived')::boolean=false;
 if t is null then raise exception 'No active tenant';end if;
 if payment_date<(t->>'moveIn')::date or payment_date>(now() at time zone 'Asia/Manila')::date then raise exception 'Invalid payment date';end if;
 remaining:=requested;
 for item in select x.value,(x.ordinality-1)::integer as idx from jsonb_array_elements(ledger->'bills') with ordinality x where x.value->>'tenantId'=tid order by x.value->>'due',x.value->>'id'
 loop
  b:=item.value;
  bill_sum:=(b->>'rent')::bigint+(b->>'electric')::bigint+(b->>'water')::bigint+(b->>'other')::bigint;
  used:=least(remaining,bill_sum-(b->>'paid')::bigint);
  if used>0 then
   b:=jsonb_set(b,'{paid}',to_jsonb((b->>'paid')::bigint+used));
   ledger:=jsonb_set(ledger,array['bills',item.idx::text],b);
   allocations:=allocations||jsonb_build_array(jsonb_build_object('billId',b->>'id','amount',used,'billSnapshot',b));
   remaining:=remaining-used;
  end if;
 end loop;
 t:=jsonb_set(t,'{credit}',to_jsonb((t->>'credit')::bigint+remaining));
 ledger:=jsonb_set(ledger,array['tenants',tidx::text],t);
 select coalesce(sum((x->>'rent')::bigint+(x->>'electric')::bigint+(x->>'water')::bigint+(x->>'other')::bigint-(x->>'paid')::bigint),0)
 into outstanding from jsonb_array_elements(ledger->'bills') x where x->>'tenantId'=tid;
 now_text:=to_char(now() at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
 payment:=jsonb_build_object('id',request_id,'receipt','PM-'||extract(year from payment_date)::text||'-'||lpad((jsonb_array_length(ledger->'payments')+1)::text,5,'0'),
 'tenantId',tid,'houseId',h->>'id','tenantName',t->>'name','houseLabel',h->>'label','amount',requested,
 'date',payment_date::text,'recordedAt',now_text,'recordedBy',name,'notes',coalesce(payload->>'notes',''),
 'allocations',allocations,'credit',remaining,'balanceAfter',outstanding,'voided',false);
 ledger:=jsonb_set(ledger,'{payments}',(ledger->'payments')||jsonb_build_array(payment));
 ledger:=jsonb_set(ledger,'{audit}',(ledger->'audit')||jsonb_build_array(jsonb_build_object('action','payment','date',now_text,'by',name,'recordId',h->>'id')));
 ledger:=jsonb_set(ledger,'{version}',to_jsonb(v+1));
 update public.rental_workspace set data=ledger,version=v+1,updated_at=now() where id=1;
 return jsonb_build_object('state',ledger,'payment',payment);
end;
$$;
revoke all on function rental_private.record_rental_payment(bigint,jsonb) from public,anon;
grant execute on function rental_private.record_rental_payment(bigint,jsonb) to authenticated;
create function public.rental_record_payment(expected_version bigint,payload jsonb)
returns jsonb language sql security invoker set search_path='' as $$
 select rental_private.record_rental_payment(expected_version,payload);
$$;
revoke all on function public.rental_record_payment(bigint,jsonb) from public,anon;
grant execute on function public.rental_record_payment(bigint,jsonb) to authenticated;

insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values('paupahan-documents','paupahan-documents',false,3145728,array['application/pdf','image/jpeg','image/png']);
create policy rental_family_files_read on storage.objects for select to authenticated
 using(bucket_id='paupahan-documents' and (select rental_private.rental_role()) is not null);
create policy rental_owner_files_insert on storage.objects for insert to authenticated
 with check(bucket_id='paupahan-documents' and (select rental_private.rental_role())='owner');
create policy rental_owner_files_delete on storage.objects for delete to authenticated
 using(bucket_id='paupahan-documents' and (select rental_private.rental_role())='owner');

insert into public.rental_workspace(id,version,data) values(1,0,'{"version": 0, "settings": {"electricRate": 2500, "waterRate": 7000, "name": "Paupahan Manager", "address": "Rental property"}, "houses": [{"id": "H01", "label": "House 01", "rent": 400000, "tenantId": null}, {"id": "H02", "label": "House 02", "rent": 400000, "tenantId": null}, {"id": "H03", "label": "House 03", "rent": 400000, "tenantId": null}, {"id": "H04", "label": "House 04", "rent": 400000, "tenantId": null}, {"id": "H05", "label": "House 05", "rent": 400000, "tenantId": null}, {"id": "H06", "label": "House 06", "rent": 400000, "tenantId": null}, {"id": "H07", "label": "House 07", "rent": 400000, "tenantId": null}, {"id": "H08", "label": "House 08", "rent": 400000, "tenantId": null}, {"id": "H09", "label": "House 09", "rent": 400000, "tenantId": null}, {"id": "H10", "label": "House 10", "rent": 400000, "tenantId": null}], "tenants": [], "bills": [], "payments": [], "audit": []}'::jsonb);
-- Approve the actual Owner email privately through the database setup workflow; never commit personal emails.
