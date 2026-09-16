"""Title-first analysis rendering keeps recommendation details behind native disclosure."""
from tests.test_tags_frontend import run_node


def test_recommendations_start_collapsed_with_stable_identity_and_complete_details():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import {stableKey} from "./web/assets/dashboard-state.js";
const source=fs.readFileSync("./web/assets/analysis.js","utf8");
const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const make=new Function("esc","money","stableKey","analysis","badge","evidenceTable","followupFor",
  source.match(/function findingCard\(f, i\) \{[\s\S]*?\n\}/)[0]+";return findingCard;");
const card=make(esc,(value,currency)=>`${currency} ${value}`,stableKey,{days:30},
  (kind,value)=>`${kind}:${value}`,()=>"<table>Evidence</table>",()=>"Explain this recommendation");
const finding={title:'Review <resources> & costs',kind:"waste",tab:"savings",currency:"USD",
  impact:12,impact_period:"window",severity:"high",confidence:"medium",source:"Savings",effort:"Low",
  detail:"Recommendation details",because:"Why it matters",risk:"Review before acting",
  steps:["Confirm ownership"],caveat:"Costs are estimates",basis:"Measured source rows"};
const html=card(finding,0), summary=html.match(/<summary[\s\S]*?<\/summary>/)[0];
assert.match(html,/^<details[^>]*name="analysis-findings"/);
assert.doesNotMatch(html.match(/^<details[^>]*>/)[0],/\bopen\b/);
assert.match(summary,/<h3>Review &lt;resources&gt; &amp; costs<\/h3>/);
for(const text of ["Recommendation details","USD 12","severity:high","Why it matters","Review before acting",
  "Confirm ownership","Costs are estimates","Measured source rows","<table>Evidence</table>","Ask about this"]){
  assert.ok(html.includes(text),`Retain ${text}`);
  assert.ok(!summary.includes(text),`Keep ${text} out of the collapsed title`);
}
const identity=value=>value.match(/data-state-key="([^"]+)"/)[1];
assert.equal(identity(card(finding,0)),identity(card(finding,4)),"row position is not the finding identity");
assert.notEqual(identity(card(finding,0)),identity(card({...finding,currency:"EUR"},0)));
assert.match(card({...finding,impact:0},0),/No saving claimed/);
assert.doesNotMatch(card({...finding,impact:0},0),/USD 0/);
assert.match(card({...finding,impact_period:"year"},0),/per year/);
assert.ok(html.indexOf('class="an-block an-block-risk"') > html.indexOf('class="an-caveat"'),
  "the warning follows the complete main message");
assert.ok(html.indexOf('class="an-block an-block-risk"') < html.indexOf('class="an-foot"'),
  "precautions remain visible before the follow-up actions");
assert.doesNotMatch(card({...finding,because:"",risk:""},0),/class="an-reasoning"|an-block-risk/,
  "missing reasoning or precautions leave no empty layout blocks");
""")
