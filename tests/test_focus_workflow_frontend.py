"""The single cost-data journey: pure review, explicit approval, scoped durable progress."""
import runpy
from pathlib import Path

_helpers = runpy.run_path(str(Path(__file__).with_name("test_tags_frontend.py")))
run_node = _helpers["run_node"]
HARNESS = _helpers["HARNESS"] + r"""
import {createTabs as createReports} from "./web/assets/dashboard-reports.js";
Element.prototype.appendChild=function(child){
  for(const parent of registry.values())parent.children=parent.children.filter(node=>node!==child);
  this.children.push(child);return child;
};
const chooseCadence=value=>$("cdCadence-"+value).onchange();
Object.defineProperty(Element.prototype,"disabled",{
  get(){return this.hasAttribute("disabled");},
  set(value){if(value)this.attrs.disabled="";else delete this.attrs.disabled;},
});
Object.defineProperty(Element.prototype,"innerHTML",{
  get(){return this.html || "";},
  set(markup){
    for(const child of this.querySelectorAll("*")){
      child.isConnected=false;
      if(registry.get(child.id)===child)registry.delete(child.id);
    }
    this.html=markup;this.children=[];
    const stack=[this],voids=new Set(["INPUT","BR","HR","IMG"]);
    for(const match of markup.matchAll(/<\/?([a-z][a-z0-9-]*)\b([^>]*)>/gi)){
      if(match[0].startsWith("</")){if(stack.length>1)stack.pop();continue;}
      const attrs={};
      for(const a of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[a[1]]=decode(a[2] || "");
      const el=new Element(match[1],attrs);
      stack.at(-1).children.push(el);
      if(el.tagName==="OPTION"){
        const select=stack.findLast(node=>node.tagName==="SELECT");
        if(select){select.options.push(el);if(select.options.length===1 || el.hasAttribute("selected"))select.value=el.value;}
      }
      if(!voids.has(el.tagName))stack.push(el);
    }
  },
});
const writes=[],feedback=[],emitted=[];
let selectedScope=["sub-a","sub-b"],failRead=null,failCatalog=null,writeReply=null;
const timers=new Map();let timerId=0;
globalThis.setTimeout=(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId;};
globalThis.clearTimeout=id=>timers.delete(id);
globalThis.CustomEvent=class{constructor(type,options){this.type=type;this.detail=options.detail;}};
globalThis.window={dispatchEvent:event=>emitted.push(event)};
const sub=(id,name)=>({id,name,configured:false,status:"idle",config:{},tasks:[],
  coverage:{authoritative_days:0,requested_days:90,missing_days:90},
  history:{from:"2026-06-18",to:"2026-09-15"}});
let fixture={can_review:true,setup_needed:true,auto_enabled:true,
  subscriptions:[sub("sub-a","Alpha"),sub("sub-b","Beta")],
  cadences:{Daily:"Daily at 02:00 UTC; 7 recent days.",Weekly:"Mondays at 02:00 UTC; 14 recent days.",
    Monthly:"Previous completed calendar month; up to a month lag."},
  note:"Existing costs stay available; Quick data is not FOCUS coverage."};
let catalog=[{id:"sub-a",name:"Alpha"},{id:"sub-b",name:"Beta"},{id:"outside",name:"Outside scope"}];
const reply=data=>({ok:true,json:async()=>data});
const previewPlan=()=>({can_start:true,from:"2026-06-18",to:"2026-09-15",preview_token:"reviewed-etag",
  identity:"Persistent workload identity",explanation:"Weekly: Mondays at 02:00 UTC, 14-day overlap.",
  notes:["External and legacy exports are untouched.","No Owner grants. Existing data stays available."],
  subscriptions:fixture.subscriptions.map(s=>({subscription:s.id,name:s.name,action:"Pause owned native cadence",
    export:{name:"owned-"+s.id,etag:"v1"},destination:{account_id:"offline",container:"focus",folder:s.id},blockers:[]}))});
ctx.readStore=(_,fallback)=>fallback;ctx.writeStore=()=>{throw Error("No financial data persistence");};
ctx.whoAmI=()=>{throw Error("Role inspection must be lazy");};
ctx.allSubscriptions=()=>[{id:"outside",name:"Outside scope"}];
ctx.scopeIds=()=>selectedScope;ctx.toast=(...args)=>feedback.push(args);
ctx.get=async(url,options)=>{
  requests.push(url);
  if(url==="/api/cost-data/subscriptions"){if(failCatalog)throw failCatalog;return {subscriptions:structuredClone(catalog)};}
  if(failRead)throw failRead;
  const scope=new URL(url,"https://offline.invalid").searchParams.get("scope")?.split(",") || [];
  return structuredClone({...fixture,subscriptions:fixture.subscriptions.filter(s=>scope.includes(s.id))});
};
globalThis.fetch=async(url,options)=>{
  const call={url,method:options.method,body:options.body?JSON.parse(options.body):null};writes.push(call);
  if(writeReply)return writeReply(call);
  return reply(url.endsWith("/preview")?previewPlan():{job_id:"durable-job",status:"queued"});
};
const reports=createReports(ctx);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const advance=async()=>{
  const entry=timers.entries().next().value;assert.ok(entry,"expected status timer");
  timers.delete(entry[0]);entry[1].callback();await flush();
};
"""


def test_initial_render_is_cached_scope_first_and_has_one_setup_flow():
    run_node(HARNESS + r"""
await reports.renderSettings(1);
assert.deepEqual(requests,["/api/cost-data/subscriptions","/api/cost-data?scope=sub-a%2Csub-b&include_export_months=1"]);
assert.equal(writes.length,0);assert.equal(timers.size,0);
assert.equal($("cdCadence-Daily").hasAttribute("checked"),true);
assert.equal((new Date($("cdTo").value)-new Date($("cdFrom").value))/86400000,89);
assert.match(root.innerHTML,/Alpha, Beta/);assert.match(root.innerHTML,/Outside scope/);
assert.equal($("sCreate"),undefined);assert.equal($("fVerify"),undefined);
assert.ok($("cdAdvanced"));assert.ok(!$("cdInventory").open);
assert.ok(root.innerHTML.indexOf('id="cdSetup"') < root.innerHTML.indexOf('id="cdStatus"'),
  "First-run setup must lead, rather than sit below status and export inventory");
assert.ok(!$("cdHistoryDates").open,"The 90-day default must not require date entry");
assert.match($("cdCadenceHelp").textContent,/Checks every day/);
assert.match($("cdTechnical").innerHTML,/02:00 UTC/);
reports.stop();
""")


def test_review_cancel_and_one_confirmed_scoped_write_keep_preview_token():
    run_node(HARNESS + r"""
selectedScope=["sub-a","sub-b"];
await reports.renderSettings(1);
chooseCadence("Weekly");
await $("cdReview").onclick();
assert.equal(writes.length,1);assert.equal(writes[0].url,"/api/cost-data/preview");
assert.deepEqual(writes[0].body.scope,["sub-a","sub-b"]);
assert.match($("cdPlan").innerHTML,/External and legacy exports are untouched/);
const cancelled=$("cdStart").onclick;$("cdCancel").onclick();await cancelled();
assert.equal(writes.length,1);
await $("cdReview").onclick();
const start=$("cdStart").onclick;await start();await start();
assert.equal(writes.filter(w=>w.url==="/api/cost-data/setup").length,1);
const body=writes.at(-1).body;
assert.equal(body.preview_token,"reviewed-etag");assert.equal(body.cadence,"Weekly");
assert.ok(body.idempotency_key);assert.equal(body.action,"setup");
assert.match($("cdNotice").textContent,/queued/);
reports.stop();
""")


def test_reader_can_see_status_but_not_write():
    run_node(HARNESS + r"""
fixture.can_review=false;await reports.renderSettings(1);
assert.equal($("cdReview").disabled,true);assert.equal($("cdRetry").disabled,true);
await $("cdReview").onclick();assert.equal(writes.length,0);
assert.match(root.innerHTML,/administrator with Azure export write access/);
assert.match($("cdStatus").innerHTML,/Quick data/);
reports.stop();
""")


def test_etag_permission_blockers_prevent_approval_not_read_access():
    run_node(HARNESS + r"""
writeReply=()=>reply({...previewPlan(),can_start:false,subscriptions:[{
  ...previewPlan().subscriptions[0],blockers:["Owned export ETag unavailable","Workload Blob read access missing"]
}]});
await reports.renderSettings(1);await $("cdReview").onclick();
assert.equal($("cdStart").disabled,true);
assert.match($("cdPlan").innerHTML,/Workload Blob read access missing/);
await $("cdStart").onclick();assert.equal(writes.length,1);
reports.stop();
""")


def test_scope_dates_and_cached_resume_invalidate_approvals():
    run_node(HARNESS + r"""
await reports.renderSettings(1);await $("cdReview").onclick();
const stale=$("cdStart").onclick;
$("cdFrom").value="2026-05-01";$("cdFrom").onchange();await stale();
assert.equal(writes.length,1);
await $("cdReview").onclick();const cached=$("cdStart").onclick;
reports.stop();reports.resume(1,"settings");await cached();
assert.equal(writes.length,2);assert.equal($("cdStart"),undefined);
await $("cdReview").onclick();const scoped=$("cdStart").onclick;
await $("cdNone").onclick();await scoped();assert.equal(writes.length,3);
reports.stop();
""")


def test_late_preview_cannot_replace_newer_review():
    run_node(HARNESS + r"""
let finish;writeReply=call=>call.body.cadence==="Daily"?new Promise(resolve=>finish=resolve):reply(previewPlan());
await reports.renderSettings(1);const pending=$("cdReview").onclick();
chooseCadence("Weekly");await $("cdReview").onclick();
const approved=$("cdStart");finish(reply({...previewPlan(),preview_token:"old"}));await pending;
assert.equal($("cdStart"),approved);reports.stop();
""")


def test_progress_preserves_last_good_and_reports_pending_not_success():
    run_node(HARNESS + r"""
fixture.subscriptions[0].status="waiting-for-Azure";
fixture.subscriptions[0].through="2026-08-31";
await reports.renderSettings(1);assert.equal(timers.size,1);
assert.match($("cdStatus").innerHTML,/Awaiting Azure/);
const previous=$("cdStatus").innerHTML;failRead=Error("network");
await $("cdCheck").onclick();
assert.equal($("cdStatus").innerHTML,previous);
assert.match($("cdNotice").textContent,/Last known costs/);
assert.equal(emitted.length,0);assert.equal(writes.length,0);
reports.stop();assert.equal(timers.size,0);
""")


def test_partial_retry_submits_once_per_job_and_existing_data_remains_visible():
    run_node(HARNESS + r"""
for(const s of fixture.subscriptions){s.status="partial";s.job_id="same-job";s.retry_job_id="same-job";s.configured=true;}
fixture.subscriptions[0].coverage.authoritative_days=30;
await reports.renderSettings(1);await $("cdRetry").onclick();
assert.equal(writes.length,1);assert.equal(writes[0].url,"/api/focus/jobs/same-job/resume?scope=sub-a%2Csub-b");
assert.match($("cdStatus").innerHTML,/30 of 180/);
assert.match($("cdNotice").textContent,/already completed windows will not run again/);
reports.stop();
""")


def test_lost_write_response_reuses_idempotency_key():
    run_node(HARNESS + r"""
writeReply=call=>call.url.endsWith("/preview")?reply(previewPlan()):
  {ok:false,json:async()=>({detail:"Response unavailable"})};
await reports.renderSettings(1);await $("cdReview").onclick();
const start=$("cdStart").onclick;await start();await start();
const starts=writes.filter(w=>w.url.endsWith("/setup"));
assert.equal(starts.length,2);assert.equal(starts[0].body.idempotency_key,starts[1].body.idempotency_key);
assert.match($("cdNotice").textContent,/Check status before retrying/);reports.stop();
""")


def test_inventory_is_explicit_lazy_read_only_and_escaped():
    run_node(HARNESS + r"""
await reports.renderSettings(1);assert.equal(requests.length,2);
fixture.schedules=[{name:"<img>",subscription_id:"sub-a",owned:false,status:"Active"}];
await $("cdLoadExports").onclick();
assert.equal(requests.length,3);assert.match(requests[2],/api\/schedules/);
assert.match($("cdExports").innerHTML,/Legacy \/ external/);
assert.doesNotMatch($("cdExports").innerHTML,/<img>|data-del|data-edit|data-run/);
assert.equal(writes.length,0);reports.stop();
""")


def test_header_refresh_deep_links_to_same_setup_without_azure_discovery():
    run_node(HARNESS + r"""
selectedScope=["sub-b"];
const result=await reports.refreshLatest();
assert.equal(result.status,"setup");assert.deepEqual(result.scope,["sub-b"]);
assert.equal(emitted[0].type,"cl:goto-tab");assert.equal(emitted[0].detail,"settings");
assert.equal(requests.length,0);assert.equal(writes.length,0);reports.stop();
""")


def test_post_setup_frequency_and_monthly_refresh_reuse_same_review():
    run_node(HARNESS + r"""
fixture.setup_needed=false;
for(const s of fixture.subscriptions){s.configured=true;s.config={cadence:"Monthly"};
 s.coverage={authoritative_days:90,requested_days:90,missing_days:0};s.status="ready";}
await reports.renderSettings(1);assert.equal($("cdCadence-Monthly").hasAttribute("checked"),true);
assert.equal($("cdSetup").hidden,false);
assert.equal($("cdPanel-schedule").hidden,true);
$("cdTab-schedule").onclick();await $("cdChange").onclick();
assert.equal(writes.at(-1).body.action,"schedule");
assert.equal(writes.at(-1).body.start,undefined);assert.equal(writes.at(-1).body.history_days,undefined);
$("cdCancel").onclick();await $("cdRefresh").onclick();
assert.equal(writes.at(-1).body.action,"refresh");assert.equal(writes.at(-1).body.cadence,"Monthly");
reports.stop();
""")


def test_advanced_reconciliation_lets_customer_choose_dates_before_review():
    run_node(HARNESS + r"""
fixture.setup_needed=false;
for(const s of fixture.subscriptions){s.configured=true;s.config={cadence:"Daily"};}
await reports.renderSettings(1);$("cdReconcile").onclick();
assert.equal(writes.length,0);assert.equal($("cdHistory").hidden,false);
$("cdFrom").value="2026-05-01";$("cdTo").value="2026-05-31";
$("cdFrom").onchange();await $("cdReview").onclick();
assert.equal(writes[0].body.action,"reconcile");
assert.equal(writes[0].body.start,"2026-05-01");assert.equal(writes[0].body.end,"2026-05-31");
reports.stop();
""")


def test_local_ingest_catalog_and_empty_selection_never_mean_all():
    run_node(HARNESS + r"""
selectedScope=["sub-a"];
await reports.renderSettings(1);
assert.match(root.innerHTML,/Outside scope/);
assert.match(requests.at(-1),/scope=sub-a$/);
await $("cdReview").onclick();const stale=$("cdStart").onclick;
await $("cdNone").onclick();const count=requests.length;
assert.match($("cdScopeNote").innerHTML || root.innerHTML,/Choose at least one subscription/);
for(const id of ["cdReview","cdRefresh","cdChange","cdRetry","cdLoadExports","pInspect"])assert.equal($(id).disabled,true,id);
await stale();await $("cdReview").onclick();await $("cdCheck").onclick();
await $("cdLoadExports").onclick();await $("pInspect").onclick();
assert.equal(requests.length,count);assert.equal(writes.length,1);
assert.deepEqual(selectedScope,["sub-a"],"Ingest choices do not mutate cost-view filters");
fixture.subscriptions.push(sub("outside","Outside scope"));
await $("cdAll").onclick();
assert.match(requests.at(-1),/scope=outside%2Csub-a%2Csub-b$/);
assert.deepEqual(selectedScope,["sub-a"]);
reports.stop();
""")


def test_ingest_picker_is_a_searchable_menu_that_survives_its_own_selection():
    """Ticking a box re-reads status for the new scope, which rebuilds the card. The menu, the
    search text and the filtered rows have to come back, or choosing a second subscription
    means reopening the menu and retyping the search every single time."""
    run_node(HARNESS + r"""
selectedScope=["sub-a"];
await reports.renderSettings(1);
assert.equal($("cdSubsMenu").hidden,true,"the list is a menu, closed until asked for");
assert.equal($("cdSubsToggle").getAttribute("aria-expanded"),"false");
assert.match(root.innerHTML,/id="cdSubsSummary">1 of 3 selected</);
$("cdSubsToggle").onclick({stopPropagation(){}});
assert.equal($("cdSubsMenu").hidden,false);
assert.equal($("cdSubsToggle").getAttribute("aria-expanded"),"true");
$("cdSearch").value="Beta";$("cdSearch").oninput();
const rows=()=>Object.fromEntries([...$("cdSubs").querySelectorAll("[data-ingest-row]")]
  .map(r=>[r.dataset.ingestRow,r.hidden]));
assert.deepEqual(rows(),{"sub-a":true,"sub-b":false,outside:true});
assert.equal($("cdSubsEmpty").hidden,true);
$("cdSearch").value="nothing matches";$("cdSearch").oninput();
assert.equal($("cdSubsEmpty").hidden,false,"an empty result says so rather than looking broken");
$("cdSearch").value="Beta";$("cdSearch").oninput();
const box=[...$("cdSubs").querySelectorAll("[data-ingest-sub]")].find(b=>b.dataset.ingestSub==="sub-b");
box.checked=true;await box.onchange();
assert.equal($("cdSubsMenu").hidden,false,"the menu stays open across the rebuild");
assert.equal($("cdSearch").value,"Beta","the search text survives the rebuild");
assert.deepEqual(rows(),{"sub-a":true,"sub-b":false,outside:true});
assert.match(root.innerHTML,/id="cdSubsSummary">2 of 3 selected</);
assert.match(requests.at(-1),/scope=sub-a%2Csub-b$/);
assert.deepEqual(selectedScope,["sub-a"],"the menu never edits the dashboard filter");
reports.stop();
""")


def test_completed_history_can_enable_updates_without_leaving_schedule_tab():
    run_node(HARNESS + r"""
for(const s of fixture.subscriptions)s.coverage={authoritative_days:90,requested_days:90,missing_days:0};
await reports.renderSettings(1);
$("cdTab-schedule").onclick();chooseCadence("Weekly");
assert.equal($("cdChange").textContent,"Review weekly schedule");
await $("cdChange").onclick();
assert.equal(writes.length,1,"review stays a single pure preview");
assert.equal(writes[0].body.action,"schedule");
assert.equal(writes[0].body.start,undefined);
assert.equal($("cdPanel-schedule").hidden,false);
assert.equal($("cdScheduleReview").children[0],$("cdReviewArea"));
assert.match($("cdPlan").innerHTML,/no historical import starts now/);
assert.doesNotMatch($("cdPlan").innerHTML,/This approval imports/);
assert.match($("cdPlan").innerHTML,/id="cdStart"[^>]*>Enable weekly updates</);
assert.equal(document.activeElement,$("cdReviewTitle"));
reports.stop();
""")


def test_history_import_remains_explicit_and_never_promises_unverified_report_reuse():
    run_node(HARNESS + r"""
for(const s of fixture.subscriptions)s.coverage={authoritative_days:90,requested_days:90,missing_days:0};
await reports.renderSettings(1);
$("cdHistoryPreset").value="365";$("cdHistoryPreset").onchange();
assert.equal($("cdSetupTitle").textContent,"Choose how much history to import");
assert.equal($("cdReview").textContent,"Review import");
assert.match($("cdInitialCadence").textContent,/This import also authorizes/);
await $("cdReview").onclick();
assert.match($("cdPlan").innerHTML,/This approval imports 365 days/);
assert.match($("cdPlan").innerHTML,/id="cdStart"[^>]*>Start import and updates</);
reports.stop();
""")


def test_previous_failure_does_not_lock_customer_out_of_fresh_review():
    run_node(HARNESS + r"""
for(const s of fixture.subscriptions){
  s.coverage={authoritative_days:90,requested_days:90,missing_days:0};
  s.status="partial";s.detail="Azure returned 403: the workload identity cannot write exports.";
}
await reports.renderSettings(1);
$("cdTab-schedule").onclick();chooseCadence("Monthly");
assert.equal($("cdChange").disabled,false);
assert.match($("cdDraftNote").textContent,/previous errors do not prevent a new review/);
assert.match($("cdStatus").innerHTML,/Azure returned 403/);
assert.doesNotMatch($("cdStatus").innerHTML,/Permission needed/);
await $("cdChange").onclick();
assert.equal(writes.length,1);assert.equal(writes[0].body.action,"schedule");
assert.equal($("cdTab-schedule").getAttribute("aria-selected"),"true");
assert.equal($("cdStart").disabled,false);
assert.match($("cdPlan").innerHTML,/Enable monthly updates/);
reports.stop();
""")


def test_a_configured_selection_still_edits_frequency_without_touching_history():
    run_node(HARNESS + r"""
for(const s of fixture.subscriptions){
  s.configured=true;s.config={cadence:"Daily",enabled:true};
  s.coverage={authoritative_days:90,requested_days:90,missing_days:0};
}
await reports.renderSettings(1);
assert.equal($("cdChange").textContent,"Review daily schedule");
assert.match($("cdDraftNote").textContent,/does not start a historical import/);
chooseCadence("Weekly");
await $("cdChange").onclick();
assert.equal(writes.length,1);
assert.equal(writes[0].body.action,"schedule");
assert.equal(writes[0].body.cadence,"Weekly");
assert.equal(writes[0].body.start,undefined,"a frequency change carries no history window");
reports.stop();
""")

def test_schedule_review_does_not_depend_on_hidden_history_fields():
    run_node(HARNESS + r"""
await reports.renderSettings(1);
$("cdHistoryDays").value="366";$("cdHistoryDays").oninput();
$("cdTab-schedule").onclick();chooseCadence("Monthly");
assert.equal($("cdReview").disabled,true);
assert.equal($("cdChange").disabled,false);
await $("cdChange").onclick();
assert.deepEqual(writes[0].body,{scope:["sub-a","sub-b"],action:"schedule",cadence:"Monthly"});
reports.stop();
""")


def test_blocked_schedule_can_recheck_inline_and_confirm_once():
    run_node(HARNESS + r"""
let denied=true;
writeReply=call=>reply(call.url.endsWith("/preview") ? {...previewPlan(),can_start:!denied,
  subscriptions:previewPlan().subscriptions.map(s=>({...s,blockers:denied?["Storage network request failed"]:[]}))}
  : {job_id:"configured-job"});
await reports.renderSettings(1);$("cdTab-schedule").onclick();chooseCadence("Weekly");
await $("cdChange").onclick();
assert.equal($("cdStart").disabled,true);
assert.match($("cdPlan").innerHTML,/Storage network request failed/);
assert.equal($("cdPanel-schedule").hidden,false);
denied=false;await $("cdRecheck").onclick();
const submit=$("cdStart").onclick;await submit();await submit();
assert.equal(writes.filter(c=>c.url.endsWith("/setup")).length,1);
assert.equal(writes.at(-1).body.action,"schedule");
assert.equal(writes.at(-1).body.preview_token,"reviewed-etag");
assert.match($("cdNotice").textContent,/not yet enabled/);
reports.stop();
""")


def test_schedule_approval_is_invalidated_by_radio_or_scope_changes():
    run_node(HARNESS + r"""
await reports.renderSettings(1);$("cdTab-schedule").onclick();
await $("cdChange").onclick();const old=$("cdStart").onclick;
chooseCadence("Weekly");await old();assert.equal(writes.length,1);
await $("cdChange").onclick();const scoped=$("cdStart").onclick;
await $("cdNone").onclick();await scoped();
assert.equal(writes.length,2);assert.equal($("cdChange").disabled,true);
assert.match($("cdDraftNote").textContent,/Select at least one subscription/);
reports.stop();
""")


def test_schedule_view_and_recovery_are_available_to_readers_without_writes():
    run_node(HARNESS + r"""
fixture.can_review=false;fixture.subscriptions[0].status="failed";
await reports.renderSettings(1);$("cdTab-schedule").onclick();
assert.equal($("cdChange").disabled,true);
await $("cdChange").onclick();assert.equal(writes.length,0);
$("cdFixes").onclick();assert.equal($("cdPanel-access").hidden,false);
$("cdBackToSchedule").onclick();assert.equal($("cdPanel-schedule").hidden,false);
assert.equal(writes.length,0);
reports.stop();
""")


def test_history_presets_custom_bounds_invalidate_immediately_and_survive_polling():
    run_node(HARNESS + r"""
await reports.renderSettings(1);
for(const days of [1,30,90,180,365]){
  $("cdHistoryPreset").value="custom";$("cdHistoryDays").value=String(days);$("cdHistoryDays").oninput();
  assert.equal($("cdReview").disabled,false);
  assert.equal((new Date($("cdTo").value)-new Date($("cdFrom").value))/86400000+1,days);
}
await $("cdReview").onclick();const stale=$("cdStart").onclick;
for(const invalid of ["0","-1","1.5","366",""]){
  $("cdHistoryDays").value=invalid;$("cdHistoryDays").oninput();
  assert.equal($("cdReview").disabled,true);await $("cdReview").onclick();
}
await stale();assert.equal(writes.length,1);
$("cdHistoryDays").value="365";$("cdHistoryDays").oninput();
const from=$("cdFrom").value;
await $("cdCheck").onclick();
assert.equal($("cdHistoryDays").value,"365");assert.equal($("cdFrom").value,from);
await reports.renderSettings(1);
assert.equal($("cdHistoryDays").value,"365");assert.equal($("cdFrom").value,from);
await $("cdReview").onclick();assert.equal(writes.at(-1).body.history_days,365);
assert.match($("cdPlan").innerHTML,/365 days and enables Daily automatic updates/);
assert.match(root.innerHTML,/Existing data is not deleted/);
reports.stop();
""")


def test_local_tabs_have_keyboard_navigation_and_no_import_side_effects():
    run_node(HARNESS.replace("const reports=createReports(ctx);",
                            "let roles=0;ctx.whoAmI=async()=>{roles++;return {user:{admin:true}};};const reports=createReports(ctx);") + r"""
await reports.renderSettings(1);
assert.equal($("cdTab-first").getAttribute("aria-selected"),"true");
assert.equal($("cdPanel-first").hidden,false);
assert.equal($("cdPanel-access").hidden,true);
const arrow=key=>({key,preventDefault(){}});
$("cdTab-first").onkeydown(arrow("ArrowRight"));
assert.equal(document.activeElement,$("cdTab-schedule"));
assert.equal($("cdTab-first").getAttribute("tabindex"),"-1");
$("cdTab-schedule").onkeydown(arrow("End"));await flush();
assert.equal(document.activeElement,$("cdTab-access"));assert.equal(roles,1);
assert.equal(requests.length,2);assert.equal(writes.length,0);
$("cdTab-access").onkeydown(arrow("Home"));
assert.equal(document.activeElement,$("cdTab-first"));
$("cdTab-first").onkeydown(arrow("ArrowLeft"));await flush();
assert.equal(document.activeElement,$("cdTab-access"));assert.equal(roles,1);
assert.equal(writes.length,0);reports.stop();
""")


def test_catalog_failure_and_access_revocation_are_not_all_or_empty_success():
    run_node(HARNESS + r"""
failCatalog=Error("Authentication needed");
await reports.renderSettings(1);assert.ok($("cdLoadRetry"));assert.equal($("cdReview"),undefined);
assert.match(root.innerHTML,/No import selection has been assumed/);
failCatalog=null;await $("cdLoadRetry").onclick();
await $("cdReview").onclick();const stale=$("cdStart").onclick;
catalog=[{id:"sub-b",name:"Beta"}];
await $("cdCatalogRefresh").onclick();await stale();
assert.equal(writes.length,1);assert.doesNotMatch(root.innerHTML,/Alpha/);
assert.match(requests.at(-1),/scope=sub-b$/);reports.stop();
""")


def test_rapid_local_selection_keeps_picker_usable_and_discards_old_status():
    run_node(HARNESS.replace("const reports=createReports(ctx);", r"""
let delayedStatus=null;
const originalGet=ctx.get;
ctx.get=(url,options)=>delayedStatus && url.startsWith("/api/cost-data?") ? delayedStatus(url) : originalGet(url,options);
const reports=createReports(ctx);
""") + r"""
await reports.renderSettings(1);
fixture.subscriptions.push(sub("outside","Outside scope"));
const pending=[];delayedStatus=url=>new Promise(resolve=>pending.push({url,resolve}));
$("cdSub0").checked=false;const first=$("cdSub0").onchange();
assert.ok($("cdAll"));assert.equal($("cdReview").disabled,true);
const latest=$("cdAll").onclick();assert.equal(pending.length,2);
pending[1].resolve(structuredClone(fixture));await latest;
pending[0].resolve({...structuredClone(fixture),subscriptions:[sub("sub-b","Beta")]});await first;
assert.match(root.innerHTML,/3 selected/);assert.match($("cdStatus").innerHTML,/Outside scope/);
assert.equal(writes.length,0);reports.stop();
""")


def test_previously_approved_history_is_restored_without_restarting_it():
    run_node(HARNESS + r"""
fixture.setup_needed=false;
for(const s of fixture.subscriptions){
 s.configured=true;s.status="ready";s.config={version:2,cadence:"Weekly",enabled:true,
  history:{from:"2025-09-16",to:"2026-09-15"}};
}
await reports.renderSettings(1);
assert.equal($("cdHistoryDays").value,"365");
assert.equal($("cdFrom").value,"2025-09-16");
assert.equal($("cdTo").value,"2026-09-15");
assert.equal($("cdCadence-Weekly").hasAttribute("checked"),true);
assert.equal(writes.length,0);reports.stop();
""")
