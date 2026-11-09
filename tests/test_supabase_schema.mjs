import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const {PGlite} = require('@electric-sql/pglite');
const db = new PGlite();
const a = '11111111-1111-4111-8111-111111111111';
const b = '22222222-2222-4222-8222-222222222222';
const sql = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'supabase_schema.sql'), 'utf8');
const rows = async (query, params=[]) => (await db.query(query,params)).rows;
const scalar = async (query, params=[]) => Object.values((await rows(query,params))[0])[0];
const as = async id => { await db.exec(`set role authenticated; select set_config('request.jwt.claim.sub', '${id}', false);`); };
const reject = async (fn, note) => { let raised = false; try {await fn();} catch (e) {raised=true; console.log('expected rejection',note,e.message.slice(0,100));} assert.ok(raised,note); };
try {
 await db.waitReady;
 await db.exec(`create schema auth; create table auth.users (id uuid primary key, email text);
    create role anon; create role authenticated;
    create function auth.uid() returns uuid language sql stable as
    $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
    grant usage on schema auth to authenticated;
    grant execute on function auth.uid() to authenticated;
    insert into auth.users(id,email) values ('${a}','alice@test.example'), ('${b}','bob@test.example');`);
 await db.exec(sql);
 console.log('schema installed on PostgreSQL/PGlite');
 // Initial data written by a privileged administrator for two users.
 await db.exec(`insert into public.profiles(id,currency) values ('${a}','Rs'), ('${b}','€');
    insert into public.categories(user_id,name) values ('${a}','Pets'), ('${b}','Pets');
    insert into public.budgets(user_id,month,category,amount) values ('${a}','2026-11','Pets',50), ('${b}','2026-11','Pets',100);
    insert into public.transactions(user_id,txn_date,amount,kind,category,note)
    values ('${a}','2026-11-08',20,'Expense','Pets','Alice item'), ('${b}','2026-11-08',40,'Expense','Pets','Bob item');
    insert into public.goals(user_id,name,target,saved,due_date) values ('${b}','Bob goal',100,0,'2027-01-01');`);
 await db.exec(sql); // repeated deployment must preserve rows
 assert.equal(await scalar('select count(*)::int from public.transactions'), 2);
 console.log('reapplying schema retained both users\' existing rows');
 const bobGoalId = await scalar(`select id from public.goals where name='Bob goal'`);

 await as(a);
 assert.equal(await scalar('select count(*)::int from public.transactions'),1);
 assert.equal(await scalar('select currency from public.profiles'), 'Rs');
 await reject(()=>db.exec(`insert into public.budgets(user_id,month,category,amount) values ('${b}','2026-12','Food',5)`),'RLS blocks writing another user');
 await reject(()=>db.exec(`update public.profiles set id='${b}' where id='${a}'`),'RLS blocks moving profile to other user');
 await reject(()=>db.exec(`insert into public.transactions(user_id,txn_date,amount,kind,category,goal_id)
    values ('${a}','2026-11-08',5,'Expense','Savings contribution',${bobGoalId})`),'RLS blocks linking another user’s goal');
 await db.exec(`select public.rename_user_category('Pets','Animals');`);
 assert.equal(await scalar('select category from public.transactions'), 'Animals');
 assert.equal(await scalar('select category from public.budgets'), 'Animals');
 assert.equal(await scalar('select name from public.categories'), 'Animals');
 await db.exec(`select public.create_user_goal('Trip',100,10,'2027-01-01'::date,'2026-11-08'::date)`);
 const goal = await scalar(`select id from public.goals where name='Trip'`);
 assert.equal(await scalar(`select saved::int from public.goals where id=${goal}`),10);
 const initial = await scalar(`select id from public.transactions where goal_id=${goal}`);
 await db.exec(`select public.add_goal_contribution(${goal},15,'2026-11-08'::date)`);
 assert.equal(await scalar(`select saved::int from public.goals where id=${goal}`),25);
 const contribution = await scalar(`select id from public.transactions where goal_id=${goal} and id <> ${initial}`);
 await db.exec(`select public.update_user_transaction(${contribution},'2026-11-09'::date,5,'Expense','Savings contribution','Edited')`);
 assert.equal(await scalar(`select saved::int from public.goals where id=${goal}`),15);
 await db.exec(`select public.update_user_transaction(${contribution},'2026-11-09'::date,5,'Income','Other','Detached')`);
 assert.equal(await scalar(`select saved::int from public.goals where id=${goal}`),10);
 assert.equal(await scalar(`select goal_id from public.transactions where id=${contribution}`),null);
 await db.exec(`select public.delete_user_transaction(${initial})`);
 assert.equal(await scalar(`select saved::int from public.goals where id=${goal}`),0);
 await db.exec(`select public.import_user_transactions('[{"txn_date":"2026-11-08","amount":2,"kind":"Expense","category":"Food"},{"txn_date":"2026-11-08","amount":3,"kind":"Income","category":"Salary"}]'::jsonb)`);
 const beforeFailure = await scalar('select count(*)::int from public.transactions');
 await reject(()=>db.exec(`select public.import_user_transactions('[{"txn_date":"2026-11-08","amount":2,"kind":"Expense","category":"Food"},{"txn_date":"2026-11-08","amount":-3,"kind":"Income","category":"Salary"}]'::jsonb)`),'CSV import rolls back all rows');
 assert.equal(await scalar('select count(*)::int from public.transactions'),beforeFailure);
 console.log('owner-scoped CRUD/RLS, goal accounting, atomic CSV import passed');

 const backup = {
    p_goals:[{id:55,user_id:b,name:'New trip',target:200,saved:25,due_date:'2027-01-01'}],
    p_transactions:[{user_id:b,txn_date:'2026-11-05',amount:25,kind:'Expense',category:'Savings contribution',note:'Imported',goal_id:55}],
    p_budgets:[{user_id:b,month:'2026-11',category:'Food',amount:90}],
    p_categories:[{user_id:b,name:'Books'}], p_currency:'£'
 };
 const args = [...([backup.p_transactions,backup.p_budgets,backup.p_goals,backup.p_categories].map(JSON.stringify)),backup.p_currency];
 await db.query('select public.restore_account_backup($1::jsonb,$2::jsonb,$3::jsonb,$4::jsonb,$5::text)',args);
 assert.equal(await scalar('select count(*)::int from public.transactions'),1);
 assert.equal(await scalar('select currency from public.profiles'), '£');
 assert.equal(await scalar(`select goal_id from public.transactions`),await scalar('select id from public.goals'));
 assert.equal(await scalar('select category from public.budgets'),'Food');
 const bad = structuredClone(backup);
 bad.p_transactions.push({txn_date:'2026-11-06',amount:-1,kind:'Expense',category:'Food'});
 await reject(()=>db.query('select public.restore_account_backup($1::jsonb,$2::jsonb,$3::jsonb,$4::jsonb,$5::text)',[...([bad.p_transactions,bad.p_budgets,bad.p_goals,bad.p_categories].map(JSON.stringify)),bad.p_currency]),'invalid restore rolls back previous deletes and inserts');
 assert.equal(await scalar('select count(*)::int from public.transactions'),1);
 assert.equal(await scalar('select note from public.transactions'),'Imported');
 await reject(()=>db.exec(`select public.add_goal_contribution(99999,1,'2026-11-08'::date)`),'cannot contribute to absent/foreign goal');
 console.log('atomic account restore, owner remap, reject bad backup passed');

 const importedGoalId = await scalar('select id from public.goals');
 await as(b);
 assert.equal(await scalar('select count(*)::int from public.transactions'),1);
 assert.equal(await scalar('select note from public.transactions'),'Bob item');
 assert.equal(await scalar('select currency from public.profiles'),'€');
 await reject(()=>db.exec(`select public.delete_user_transaction(${contribution})`),'other account cannot delete transaction');
 await reject(()=>db.exec(`select public.add_goal_contribution(${importedGoalId} ,1,'2026-11-08'::date)`),'other account cannot add to Alice goal');
 await db.exec(`set role anon;`);
 await reject(()=>db.exec(`select public.delete_my_account()`),'anonymous cannot invoke account deletion');
 await as(a);
 await db.exec(`select public.delete_my_account()`);
 await db.exec('reset role;');
 assert.equal(await scalar(`select count(*)::int from auth.users where id='${a}'`),0);
 for (const table of ['profiles','transactions','budgets','goals','categories']) {
  assert.equal(await scalar(`select count(*)::int from public.${table} where ${table==='profiles'?'id':'user_id'}='${a}'`),0,table);
 }
 assert.equal(await scalar(`select count(*)::int from auth.users where id='${b}'`),1);
 assert.equal(await scalar(`select count(*)::int from public.transactions where user_id='${b}'`),1);
 console.log('Auth deletion cascaded only Alice; Bob retained all rows');
 console.log('PASS local PostgreSQL (PGlite) schema/PLpgSQL/RLS integration');
} catch (err) { console.error("FAIL",err.message); process.exitCode = 1; } finally { await db.close(); }
