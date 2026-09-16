"""Permission diagnostics stay read-only, lazy and bound to the visible context."""
import runpy
from pathlib import Path

_helpers = runpy.run_path(str(Path(__file__).with_name("test_focus_workflow_frontend.py")))
run_node = _helpers["run_node"]
HARNESS = _helpers["HARNESS"].replace(
    "const reports=createReports(ctx);",
    r"""
let roleReads=0;
ctx.whoAmI=async()=>{roleReads++;return {user:{admin:true}};};
let capReply=async()=>({
  generated_at:"2026-09-14T18:00:00Z",read_only:true,
  capabilities:[{feature:"cost-query",status:"declared-allowed",
    principal:{id:"principal-one"},required_role:"Cost Management Reader",
    scope:"/subscriptions/sub-a",reason:"Declared",next_step:"No role change indicated."}]
});
const capabilityReads=[];
const baseGet=ctx.get;
ctx.get=async(url,options)=>{
  if(url.startsWith("/api/capabilities")){
    capabilityReads.push({url,options});
    return capReply(url,options);
  }
  return baseGet(url,options);
};
const reports=createReports(ctx);
""",
)


def test_permissions_are_lazy_and_reads_use_captured_scope_without_writes():
    run_node(HARNESS + r"""
selectedScope=["sub-b","sub-a"];
await reports.renderSettings(1);
assert.equal(roleReads,0,"ordinary schedule loading does not reinterpret permissions from account mode");
$("pPanel").open=true;
await $("pPanel").ontoggle();
assert.equal(roleReads,1);
assert.equal($("pWorkload").hidden,false);
await $("pInspect").onclick();
const query=new URL(capabilityReads[0].url,"https://offline.invalid").searchParams;
assert.equal(query.get("scope"),"sub-a,sub-b");
assert.equal(query.get("mode"),"caller");
assert.equal(query.get("refresh"),"true");
assert.equal(writes.length,0);
assert.equal(tables.get("Caller access declarations").rows[0].principal.id,"principal-one");
assert.match($("pStatus").textContent,/No permissions were changed/);
assert.match($("pStatus").textContent,/read-only/);
assert.equal($("pInspect").disabled,false);
reports.stop();
""")


def test_permissions_completion_cannot_overwrite_a_new_scope():
    run_node(HARNESS + r"""
await reports.renderSettings(1);
let finish;
capReply=()=>new Promise(resolve=>{finish=resolve;});
const pending=$("pInspect").onclick();
await flush();
const signal=capabilityReads[0].options.signal;
reports.stop();
assert.equal(signal.aborted,true);
selectedScope=["sub-b"];ctx.loadToken=2;
await reports.renderSettings(2);
finish({capabilities:[{feature:"old-scope",status:"denied"}]});
await pending;
assert.equal(tables.has("Caller access declarations"),false);
assert.equal($("pInspect").disabled,false);
reports.stop();
""")


def test_permissions_failure_preserves_labelled_previous_result():
    run_node(HARNESS + r"""
await reports.renderSettings(1);
$("pResult").innerHTML="<p>Previously checked caller access</p>";
capReply=async()=>{throw Error("Access denied");};
await $("pInspect").onclick();
assert.match($("pResult").innerHTML,/Previously checked caller access/);
assert.match($("pStatus").textContent,/Could not inspect permissions/);
assert.match($("pStatus").textContent,/has not been updated/);
assert.equal($("pInspect").disabled,false);
assert.equal(writes.length,0);
reports.stop();
""")


def test_permissions_remain_available_when_export_discovery_fails_and_cells_escape_text():
    run_node(HARNESS + r"""
failRead=Error("The export list is unavailable");
await reports.renderSettings(1);
assert.ok($("cdLoadRetry"));
assert.ok($("pInspect"));
await $("pInspect").onclick();
assert.equal(writes.length,0);
const columns=tables.get("Caller access declarations").columns;
const hostile={feature:"<script>x</script>",status:"denied<script>",
  principal:{id:"<img>"},required_role:"<role>",scope:"<scope>",reason:"<reason>",next_step:"<step>"};
for(const column of columns){
  const html=column.cell(hostile);
  assert.doesNotMatch(html,/<script|<img>|<role>|<scope>|<reason>|<step>/);
}
reports.stop();
""")


def test_permissions_follow_local_ingest_not_the_header_and_discard_old_results():
    run_node(HARNESS + r"""
await reports.renderSettings(1);
let finish;capReply=()=>new Promise(resolve=>finish=resolve);
const pending=$("pInspect").onclick();await flush();
const signal=capabilityReads[0].options.signal;
$("cdSub0").checked=false;await $("cdSub0").onchange();
assert.equal(signal.aborted,true);
finish({capabilities:[{feature:"stale-access",status:"denied"}]});await pending;
assert.equal(tables.has("Caller access declarations"),false);
capReply=async()=>({capabilities:[],generated_at:"now"});
await $("pInspect").onclick();
assert.equal(new URL(capabilityReads.at(-1).url,"https://offline.invalid").searchParams.get("scope"),"sub-b");
assert.deepEqual(selectedScope,["sub-a","sub-b"]);
await $("cdNone").onclick();const before=capabilityReads.length;
await $("pInspect").onclick();assert.equal(capabilityReads.length,before);
assert.equal(writes.length,0);reports.stop();
""")
