# CloudLens

**See. Analyze. Optimize.** — total clarity for your cloud spend.

An Azure cost dashboard with an agent attached. Tabs answer the questions you always have;
the chat box handles the ones you didn't expect.

Cost data is pulled once into a PostgreSQL warehouse, so queries run in milliseconds and can
slice any way you ask. Waste findings come from live Azure inventory, joined back to what each
resource actually cost.

Savings delivers findings as each of its four sources finishes. Completed scans are cached by
identity, subscription scope, period and currency. A new request after 60 seconds can return
previous results while a shared scan refreshes them; merely reopening a completed tab makes no
request. The response includes `scan` progress/errors and
`freshness` timestamps, age and stale/refreshing flags. Partial totals can change as later sources
merge duplicate findings, so they are not a final savings estimate.

Controls, maps, heatmaps and charts share local-only tooltips. Focus a control to read its hint;
on a chart, use arrow keys to read values and Escape to dismiss. Hover details do not fetch data.

Overview is the cost home page: a unified selected-period summary, quick access to AI Analysis,
Savings, Budgets and Export, and cost-driver links into the detailed tables. These signals reuse
the existing executive response; they do not start new scans or claim savings. Cost breakdowns
and estate visualizations open on demand, preserving their state during cached navigation.
The daily comparison and at-most-three-month chart keep their own labeled reporting windows.

AI Analysis presents a compact list of recommendation titles. Expand one to read its impact,
confidence, reasoning, precautions and evidence; opening another closes the previous one.
Overview/priorities and methodology are available on demand rather than repeated above the list.
Returning from a follow-up conversation or reopening the analysis retains the expanded finding.
Expansion uses already-loaded results and makes no requests; refreshing or changing scope starts
with recommendations collapsed.

Tab families load on demand. A bounded, in-memory view cache preserves open details, table
sorting/paging/search, input values, selection and scroll while navigating. Billing, Resources,
Savings and Hourly Billing group context, controls and metrics in a shared panel; metric bands,
table headers and secondary disclosures use the same visual treatment across the portal.
Savings priorities are available on demand without refetching their loaded summary.
Refreshes capture
interaction state when the new content arrives, including edits made during the request; a
different subscription scope, period, currency or filter starts a separate view.
Visited tabs retain their snapshots for the lifetime of the open page, without five-minute
expiry or automatic refetch on return. Up to 40 views are retained in the current reporting
context, covering all dashboard tabs; requests remain bounded and context-keyed. Unfinished
scans and interrupted requests can resume, and source timestamps/warnings remain visible.
**Refresh views** clears tab snapshots and reloads the selected view without importing billing
data; other tabs reload when next opened. Individual source refresh buttons still work.
Reloading the browser also clears snapshots. Results are not stored in browser local storage.
The **Cost period** filter offers rolling 7/30/60/90-day windows or an explicit calendar
month. The caption shows the inclusive dates returned by the warehouse, including both months
when a rolling window crosses a month boundary. Calendar selection is retained across reloads;
choosing a rolling preset clears it and displays that preset's actual date range in the calendar
control. Opening the calendar suggests the current month without treating it as selected.
Cost tabs, filter values, analysis and report downloads
use the selected period. Live metrics and the summary above retain their own labeled windows.
The navigation rail starts directly below the header, beside the summary and filters. The Area
selector remains available on every service-family/overlay view. Daily spend includes short
windows and keeps its calendar dates aligned to the billing dates in every browser time zone.
Rows in shared tables expand into locally loaded details, including nested tags and evidence;
their portal actions distinguish exact resources, resource groups and subscriptions. Resource
names and Savings items link directly to Azure when their ARM IDs are known. Each tab offers
selected-subscription portal choices and related CloudLens views without replacing the tab
navigation. Chart points open their values on click or Enter; existing specialized drill-downs
remain in place. Portal links open separately, and CloudLens date/dimension filters are not
transferred.

The visual system shares refined typography, softly elevated cards and clearer view/KPI
hierarchy across Light, Dark and Colourful themes. It uses local system fonts, preserves
reduced-motion preferences, and keeps the full-height rail and responsive layouts.
Views use compact title bars, tighter KPI/card spacing and 10-row table pages by default.
Long definitions are available under keyboard-accessible, collapsed help sections.
Errors, missing access/data, partial coverage and financial caveats remain visible.
Export schedules use five-row client-side pages; expand a schedule for its destination and
actions. Paging keeps form edits and open details without another Azure request.
The shared summary keeps labels, values, comparisons and freshness visible in a compact
grid. Contextual actions sit immediately left of Local data in the filter/status row.
Subtab rows use borderless tabs and an active underline without repeating the sidebar's section
name. Views without subtabs retain their heading. Smaller screens retain scrollable navigation.
Short panels and empty chart states no longer stretch to fill a
neighbor, and default chart plots scale to the available space without changing explicit
per-view heights.

```bash
pip install -r requirements.txt
az login

# Where the warehouse lives. The deployment provisions PostgreSQL 16; for a local run:
#   docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=cloudlens postgres:16
export PGHOST=localhost PGDATABASE=cloudlens PGUSER=postgres PGPASSWORD=dev PGSSLMODE=disable

export PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
export MODEL_DEPLOYMENT_NAME="gpt-4.1"

python ingest.py --months 3          # build the warehouse (a few minutes)
uvicorn app.main:app --port 8100     # http://localhost:8100
```

Against Azure Database for PostgreSQL, drop `PGPASSWORD` and set `PGSSLMODE=require` — the app
authenticates with your `az login` locally and with its managed identity when deployed, so there
is no database password anywhere. It will not start without `PGHOST`: there is no correct guess
for where your cost data lives, and a warehouse in the wrong place looks identical from outside.

First start prints a generated `admin` password to the console. For anything beyond one laptop,
set up [SSO](#sharing-it-with-a-team).

## What you get

**Change Analysis and Performance vs Spend** are read-only Monitor views. Change Analysis
shows up to 200 recent Resource Graph change records, their actor and safe infrastructure
property differences, and associated billed-cost context. Azure retains change records for
up to 14 days; source omissions, redacted values and caps are explicit. Correlation is not
proof that a change caused the spend.

Performance vs Spend samples at most 24 supported resources in deterministic ARM-ID order,
not a top-spend ranking or complete estate total. It discovers metric definitions for VMs,
App Service sites/plans, SQL databases and Storage accounts before requesting hourly Azure
Monitor observations. Counts/bytes use totals; CPU, memory and latency use unweighted
averages of returned hourly observations, not P95 or request-weighted values. VM guest
memory is unavailable without additional telemetry and is not collected by this profile.
Zero measurements remain zero; missing, unsupported, denied and incomplete observations
are separately labeled and are never interpreted as idle resources.

Both views have independent 24-hour, 7-day and 14-day windows and respect the subscription picker. The
global historical cost-month, dimension and display-currency filters do not apply.
Billed-cost context covers the UTC calendar dates intersecting the evidence window,
may lag or be partial, and keeps original currencies separate. Refresh evidence performs
no resource or permission changes. Resource Graph and Azure Monitor access must already
exist for the delegated caller, or for the app identity in non-delegated mode; these views
do not enable monitoring, create diagnostic settings, or grant themselves access.

In-view filters narrow the loaded evidence instantly: search, resource type, resource group,
billing currency and cost coverage are shared. Change Analysis also filters by change type
and actor; Performance adds region and metric coverage. Counts and tables use the same
filtered rows, and Clear filters resets the view without another Azure request. These filters
do not search beyond the 200-change or 24-resource caps; narrow the subscription picker and
refresh to retrieve a different source sample. A currency filter selects records rather than
converting or summing currencies. Recorded zero amounts remain distinct from missing costs,
and measured zero utilization remains distinct from missing telemetry.

**Billing** — last complete UTC calendar month, organized as **What did I spend? What did I
save? How much more can I save?** The historical statement loads independently of current
optimization scans. It includes billed/recorded spend, list valuation, Azure discounts,
Reservation versus Savings Plan benefits, effective discount, monthly comparison and
subscription/service/resource breakdowns. Rounding adjustments are explicit, not hidden in a
resource amount.

Invoice payments and usage economics are separate: achieved savings compare `ListCost` with
`EffectiveCost` on comparable usage and identifiable unused allocations; Azure discounts use
`ListCost - ContractedCost`, and commitment benefits use `ContractedCost - EffectiveCost`.
Upfront purchases, credits/refunds and other non-usage payments do not become fictitious usage
savings. Missing prices, source provenance, FX or comparable month coverage remain unavailable.
Current display FX is disclosed rather than presented as invoice-period exchange rates.

Future recommendations show monthly/annual estimates, evidence and review actions. Overlapping
Reservation/Savings Plan alternatives and discounts on resources proposed for removal/resizing
are excluded from the compatible headline total. The commitment watch shows provider-reported
utilization and expiry where scope and billing-reader permissions allow; billing first/last-seen
dates and resource counts are never used as substitutes. It performs no purchase or resource change.

Older imports did not retain all financial fields. The schema upgrade preserves those rows and
leaves new fields null. Use a FOCUS report refresh to supply pricing provenance; FOCUS is the
preferred source for both invoice and effective-cost evidence together. **Refresh Billing**
re-reads the statement and current estimates; it does not silently ingest reports or change
permissions. Selecting a calendar month changes Billing's statement; rolling presets leave it
on the previous calendar month. Historical
dimension filters do not apply to its current subscription-wide optimization scan.

The left rail has seven primary sections. Grouped views appear in a horizontal subsection
bar, with only the selected view rendered:

| Main section | Subsections |
| --- | --- |
| Overview | All spend and the existing cost-area selector, including available AKS/Marketplace overlays |
| Billing | Billing summary, Hourly Billing, Applications & Tags, Budgets |
| All resources | Resource explorer with local search, service/change/tag/currency filters, native-currency totals, and expandable resource details. Cost-backed records, not live inventory. |
| Savings | Savings opportunities, Reservations & Savings Plans |
| Monitor | Change Analysis, Performance vs Spend, Anomalies, History, Azure Updates |
| Governance | Compliance, End of support, Retirements, Audit log (admins only) |
| Reports | Export data, Cost exports |

**AI Billing** is an Overview view for AI service/resource costs and model-deployment usage.
Services shows billed costs by currency, observed input/output tokens, provisioned utilization
where available, and a one-line review recommendation. Models shows deployment names and model
metadata with their measured tokens and utilization. Resource-level charges are not guessed or
split into per-deployment costs; raw billing products/meters remain in service-row details.

The billing selection follows subscription, period, currency and cost filters. Azure Monitor
observations have explicit coverage and source notes; missing readings are not zero usage.
Azure Monitor returns at most 744 hourly points (31 days) per series and drops the oldest
hours beyond that, so a longer selected period reports the samples it actually observed —
744 of 2,150 for 90 days, say — and is labelled partial. It is neither summed as though it
were complete nor reported as no usage. Generic billing units and prices such as "1M Tokens"
do not establish consumed token counts.
Services and Models overlap and their token totals must not be added. Recommendations are
evidence-based review prompts, not automatic changes or guaranteed savings. Navigation and
search reuse the loaded result until **Refresh views**.

AI Billing highlights significant daily spikes **and drops** in billed resource cost, measured
tokens and provisioned utilization when comparable history exists. Flagged rows appear first;
**Only anomalies** narrows the table and **Why flagged** shows the day, observed value and
baseline. Missing/partial data and insufficient history are labelled, not reported as normal
or filled with zero. Account and deployment flags may describe the same activity.
The rule uses a trailing 14-calendar-day median/MAD baseline with at least seven complete prior
days, plus a 50% relative-change gate and materiality floors: 15 billing-currency units,
1,000 tokens or 20 utilization percentage points per day. Zero baselines use absolute/statistical
gates without a percentage. Established weekday/weekend patterns use comparable days.
Cost checks omit negative/credit days and the last two reporting dates; telemetry checks require
24 valid hourly observations for each daily value. The extra baseline does not change selected
period totals. Detection runs on AI Billing load/refresh; it does not send external alerts.

Applications & Tags partitions loaded resources into **Tagged**, **Untagged** and **Unknown**;
their counts add up to **All**. Unknown means the load did not provide tag data, not that a
resource has no tags. Status counts are independent of the tag-key/value selection. Unknown
and Untagged pause those selections and budget actions; returning to All or Tagged restores
them. Unknown spend is shown separately, and costs are summed before display rounding.

Hourly Billing shows billed hours, effective rates and an hourly average derived from daily
costs. **Billed uptime** is estimated billed-hour coverage, not monitored service availability;
high coverage does not prove a workload stayed running. Illustrative savings are conditional
VM-compute scenarios requiring workload and pricing review. App Service plans and other
unverified service types are not assigned schedule savings.

AKS discovery distinguishes detected clusters, unmatched groups and unknown inventory status.
A missing match does not prove deletion, and detected clusters can be stopped. Budget lookups
also distinguish confirmed absence from unknown readings, partial coverage and unavailable data.
Billing's **Savings vs list** compares prices for the same usage; **Potential savings** are estimates,
not guaranteed reductions or money already recovered.

Cross-links keep their existing view IDs and select the correct parent automatically.
Returning to a main section restores its last subsection during the session; a fresh
page opens Overview. Filters and cached table/form state remain attached to their original
view. Both navigation levels support arrow keys and Home/End. On narrow screens they
scroll horizontally; icon-only mode keeps all seven main sections accessible.
Overview's Area selector and the subsection bars share a compact header with borderless
buttons and an active underline. Area costs appear inline beside their labels.
The signed-in account and Sign out control live at the bottom of the sidebar, separate
from navigation rerenders. On mobile they form a bottom account bar; during first-run
setup they remain available even while the dashboard is hidden.

**Overview** — spend, the change against the previous period, daily trend, and the biggest
movers. Everything the average "how are we doing" question needs — and, at the bottom, the AKS
correction: a cluster bills the control plane and nothing else, while its node VMs, disks, load
balancers and egress sit in a managed resource group beside it, so every other view reports a
Kubernetes bill as unattributed compute. The panel attributes that spend back to the cluster
that caused it, including clusters that have been deleted and are still charging.

**Per-area tabs** — Compute, Networking, Storage, AI & Data, Integration, Security. Each with
its trend, top services, top regions and top resources.

**Rightsizing** — per-VM CPU average and peak from Azure Monitor, with power state and cost.

**Stale resources** — unattached disks, unassociated public IPs, VMs stopped but not
deallocated, empty App Service plans, old snapshots, orphaned NICs. Each with what it cost last
month, because "12 unattached disks" is trivia and "$28.50 of unattached disks" is a decision.

**End of support** — OS and SQL versions past or near end of support, and what ESU would cost.

**Reservations & Savings Plans** — pay-as-you-go, savings plan and reserved instance as three
columns, each stating what a year of the same workload costs under it rather than only what it
saves; then how spend already splits across on-demand, committed and Spot month by month, what
you already hold, which commitments are being paid for with nothing attached, and what Spot
saved against the on-demand rate for the same meter.

Deliberately its own tab rather than part of Savings. Everything under Savings is a cleanup
somebody can do this afternoon and undo tomorrow; a reservation removes nothing, re-prices what
stays, and runs for three years. When the two were merged, one Advisor recommendation appeared
as a savings card, a commitments table row and a share of the "recoverable" headline — and a
third of a total presented as recoverable needed a purchase order. Savings now excludes
commitments entirely and links here instead.

**Anomalies** — the days that broke their own pattern, against a rolling 14-day median, with the
last two days excluded because Azure restates cost data after the fact. Alerting is Azure's own:
a Cost Management scheduled action per subscription, the same mechanism a budget uses to mail its
thresholds, so nothing here needs an SMTP server and the mail keeps arriving whether or not the
app is running. The panel lists every alert across the estate, says which subscription each lives
on, names the subscriptions nobody is being emailed about, and flags the failure Azure is silent
about: an alert past its end date still shows as Enabled in the portal while sending nothing.

**Retirements** — what Azure is changing that affects you, from Service Health, sorted by deadline.

**Azure Updates** — three independently loaded sections under Monitor:
product announcements from Microsoft's [official Azure Updates RSS](https://www.microsoft.com/releasecommunications/api/v2/azure/rss),
public incidents from the [Azure Status feed](https://azure.status.microsoft/en-us/status/feed/),
and private Service Health events for the selected subscriptions. Search, category/status filters,
paging, expandable details and official-source links keep announcements actionable. The
Customer attention filter highlights retirement, security and action-required wording; it is
a relevance hint, not a claim that a customer's subscription is affected.

Public sources are cached for 10 minutes; Service Health for 2 minutes, separately by identity,
session and explicit subscription scope. Each section has its own freshness and refresh control,
and failed refreshes retain a clearly marked last-successful snapshot where available. An empty
public Status feed does **not** establish that all services are healthy. Service Health includes
incidents, maintenance and advisories, with resolved events labeled, subject to Resource Graph
retention/indexing and Azure RBAC. The existing Retirements view is unchanged.

Announcements cover the publisher's current RSS window (up to 200 items), not the entire release
history. Source or resource-list caps are disclosed. Subscription selection affects Service Health
only; cost-period, currency and dimension filters do not constrain these three sources.

**Governance** — tag coverage, untagged spend, and VMs without accelerated networking.

**Advisor** — Microsoft's own recommendations and savings estimates.

**Reports** — export any selection as Excel, PowerPoint, CSV, Markdown or JSON.

Live Azure checks and public feeds can take seconds and say what they are loading. Cost
breakdowns query the local warehouse rather than making another Azure cost request.

## Why a warehouse

The Cost Management Query API takes 2–8 seconds per call, throttles aggressively, and only
groups by its own fixed dimensions. Every breakdown is another round trip.

CloudLens imports cost exports or API results into a FOCUS-shaped table in Azure Database
for PostgreSQL Flexible Server. Dashboard grouping, comparisons and tag analysis then use
the stored data instead of repeatedly calling Azure.

PostgreSQL holds the cost history independently of App Service. Backups, monitoring and
recovery belong to the database deployment. `PGHOST` is required; there is no embedded
database fallback.

Forecasts and budgets aren't in the detail export, so those still hit the live API.

## How it fits together

```
Browser ──► FastAPI ──► gpt-4.1 (plans, explains)
                 │
                 ├─► query_costs   ──► PostgreSQL (milliseconds)
                 ├─► render_chart  ──► PostgreSQL → chart spec → Chart.js
                 ├─► find_waste    ──► Resource Graph + PostgreSQL
                 ├─► vm_utilisation──► Azure Monitor
                 └─► forecast / budgets / advisor ──► Azure (live)
```

The agent calls Python tools in-process; it does not use MCP or Foundry Agent Service.
Charts are built from tool results. Model-written explanations should still be checked
against their supporting data.

The frontend uses browser ES modules and CSS, with no bundler or CDN. Feature modules
and Chart.js load on demand. Hovering or focusing a navigation item can warm its code
module without fetching feature data; this is disabled when the browser requests data saving.
Table sorting computes display values once per row, and resource filters reuse their
calculated totals. On small screens, resource names remain visible during horizontal scrolling.

## FOCUS evidence by default

First-run, startup, daily refresh and sign-in backfill use **FOCUS** exports for billed,
effective-cost and commitment evidence. Where reports are unavailable, background loading uses
Quick ActualCost estimates without replacing richer subscription-days. Quick is not FOCUS and
does not establish complete billing or commitment coverage. Existing Actual/Amortized readers
and historical archive downloads remain supported; they are not silently substituted for a
FOCUS report refresh. Missing effective-cost or benefit attribution remains unknown.

Adding a column to `COLUMNS` migrates an existing warehouse in place rather than requiring a
re-ingest: `CREATE TABLE IF NOT EXISTS` only builds the schema on an empty database, so a
warehouse that predates a column would otherwise keep the old shape and fail every query
naming it. Existing rows get NULL, which is honest — that data genuinely was not collected.
## The map is real geography

The first version of the region view drew a graticule and no coastline, on the reasoning that a
hand-traced map would be recognisable but wrong, and people read landmasses as fact. That
reasoning was right; the answer was not to trace one.

`web/assets/worldmap.js` is generated by `tools/build_worldmap.py` from Natural Earth data
(public domain), projected and simplified once at build time into a single SVG path. Two
encodings keep it small enough to ship: relative linetos, because the delta between neighbouring
coastline points is one or two characters where an absolute coordinate is four or five; and
integer coordinates in a 2000-wide space that renders at about 1000, so rounding error is half a
pixel. That took it from 104 KB to **24 KB raw, about 10 KB gzipped** — no tiles, no CDN, no
mapping bundle.

One path for the whole world, not one per country, so the fill happens in a single operation and
shared borders cannot antialias into faint light seams.

Rings are split where they cross ±180° longitude. Without that, Russia's Chukotka and Kiribati
step from +179 to −179 between consecutive points and draw a hard horizontal line straight
across the map — which is exactly what the first build did.

Regions that are not places — Entra, Defender, marketplace, bandwidth — are counted and named in
the caption rather than plotted, because putting account-level spend somewhere on a map would be
an invention.

## Spend hierarchy

A squarified treemap, laid out against the panel's measured size rather than a notional
rectangle. Squarifying against a fixed box and then stretching the result with percentages throws
away the thing the algorithm works for: the aspect ratios it keeps near 1 get scaled by whatever
the real panel's ratio happens to be. It re-lays out on resize and drops labels that would
overflow their tile.
## The Refresh button

Two choices, because what differs is the source and detail available:

**FOCUS report** — a Cost Management export. One schema carrying actual and amortized together,
the best fidelity available. The dashboard explicitly requests `metric: "FocusCost"` at
`POST /api/exports/ingest`. Only FOCUS exports/runs qualify, including when a container SAS is
configured; downloaded files are checked before any replacement. Actual or amortized exports
are never substituted for this choice. WebDirect does not support FOCUS: use **Quick refresh**.

**Quick refresh** — a live actual-cost Query API load for the last three months, with a roughly
one-minute fetch budget. It refreshes query-only days and fills missing days, but preserves
subscription-days containing FOCUS or richer detail (including legacy tags, quantities and
pricing evidence). It never combines a query summary with detail for the same day. Use FOCUS
to update those preserved days; the completion message reports how many were preserved.

FOCUS replaces the date ranges present in the export, per selected subscription. Repeating
either refresh replaces rather than appends overlapping costs. Other subscriptions and dates
outside the replacement window stay untouched. Query pagination must finish before a
subscription is replaced: timeout, throttling or the page cap leaves its existing data unchanged.

The Query API period is fixed at three months, which is what the comparisons need. The menu used to offer
three lookback periods *and* every export it could find — nine of them here, listed by resource
name and storage account — and it fired a live Azure call on every open, so nothing was clickable
for about thirteen seconds. That is a chooser for someone who already knows which blob container
holds their data.

The generated full-detail ActualCost/AmortizedCost options are no longer in the refresh menu.
Their `/api/ingest` support and ranked export discovery when `metric` is omitted remain for
existing API clients and internal jobs. This does not change report downloads (Excel, PowerPoint,
CSV), first-run automatic setup, or daily export scheduling.

It also used to lie about the result. `ingest()` finished by hardcoding `status: "ready"`, so an
ingest where every subscription failed still reported success, and the browser — which only
tested for `"failed"` — quietly redrew the same numbers. Clicking Refresh appeared to do nothing,
with no error anywhere. It now reports `failed`, `partial` or `ready` honestly, carries the
reason, and shows it in a floating toast rather than a banner inside the tab body that the
reload immediately wipes.

When no export can be read, the reason is now one sentence rather than three exceptions spliced
together and truncated at 300 characters. Every one of those failures has the same shape — the
export's storage account is unreachable or refuses this identity — and what matters is which of
the two it is, because they need opposite fixes. In a tenant that forces
`publicNetworkAccess=Disabled` on storage, FOCUS is simply not available and the message says so
and directs the user to Quick refresh.

That check now runs **before** anything starts: one blob listing per candidate account, in
parallel, with a short timeout. Where the storage is unreachable the answer comes back in about
six seconds as a refused request, rather than as a background job that spins for half a minute
and then fails. Nothing is started, so the warehouse is not touched.

### A refresh never destroys what it cannot replace

The period being reloaded used to be deleted *first*, at the top of the job, and refilled at the
end. Between those two points — minutes, because the report is generated on Azure's side — the
dashboard read zero. If the report then failed, the data was simply gone: a run against a
subscription whose `ActualCost` report returned HTTP 500 emptied the warehouse and left it that
way.

Now the rows are fetched first and the swap is one transaction: `DELETE` and `INSERT` together,
or neither. A failed, throttled or timed-out report leaves the previous data exactly where it
was, and a reader never sees the window empty.

An empty report is treated the same way. `NoDataFound` is far more often a transient refusal than
a month that genuinely cost nothing, and deleting on that basis destroys good data to record an
absence — so the period is left alone and the run reports `empty`, which is neither the failure
it isn't nor the success it isn't either.

### Reports are queued, not fired in a burst

Cost Management rate-limits report *submission* per subscription. Running three months
concurrently meant three simultaneous submissions, two of which were refused; each then backed
off for a minute or more before its first poll. A three-month refresh took **fifteen minutes**
when it should take about as long as its slowest report.

Submissions are now serialised per subscription with a five-second gap, which stays inside the
limit. Only the POST is queued — generation and download still overlap, which is where the
minutes actually are. Different subscriptions never queue behind each other, since the limit is
not global. The same refresh now takes **four to six minutes**.

Throttling, when it still happens, is bounded in total (`SUBMIT_BUDGET`) rather than by attempt
count: six retries with an escalating backoff could sleep for twelve minutes before the first
poll, which made a doomed refresh indistinguishable from a slow one.

### Progress says what it is waiting on

`done` only moves when a whole period lands, so a refresh spent minutes showing `0/3` with
nothing else to say — and a control that merely looks busy is what makes someone press it again,
or reload the page mid-ingest and assume it failed.

A refresh now gets a **progress strip** under the header: a bar that advances, the wait named in
words, a running clock, and a line saying the dashboard stays usable. The bar is driven by a
weighted estimate — submission counts for 10% of a period, generation 45%, loading 85% — because
a bar driven by completed periods alone sits at zero for most of the run. It never goes
backwards, and never reaches 100% before the run actually ends.

The stripe animation continues even when the width does not, because the width can genuinely sit
still for two minutes while Azure builds a report, and a bar that is both still and silent is
indistinguishable from one that has died.

When Azure reports the job itself as `Failed`, the body is a correlation id and an
`InternalServerError` — which reads like a bug here and invites checking permissions, neither of
which helps. It is reported as what it is: a fault on Azure's side, naming the metric, and
pointing at the other metric, which is generated separately and often works when one does not.

## The daily archive

Every refresh writes a dated copy of what it loaded to blob storage, and the dashboard's Refresh
menu shows where it went.

Fresh deployments provision a dedicated, randomly named storage account for exports and
snapshots. Its identity is saved and reused on updates so billing history is not orphaned.
Automatic export refresh and onboarding stay within that configured account; other
deployments' exports are used only when explicitly selected. See
[deployment storage naming](infra/README.md#dedicated-export-storage) for adoption and state handling.

The warehouse is a working set, not a record: a refresh *replaces* the periods it loads, and
Azure restates cost data for several days after the fact. Without a copy taken at the time,
"what did we think this cost when we reported it?" has no answer.

```
st<20-random-hex-characters>/cloudlens/
  focus/2026/08/focus-2026-08-27.parquet
  amortized/2026/08/amortized-2026-08-27.parquet
```

**One blob per dataset per day.** The name is derived from the dataset and the date and nothing
else, so a second refresh the same day overwrites the first rather than accumulating
near-identical copies — someone pressing Refresh four times in an afternoon is correcting
something, not asking for four archives. The overwrite is just a PUT onto the same name.

**The date is the day the archive was taken**, not the last day of data in it. Because cost data
is restated, two archives taken a week apart can cover the same period and differ — which is
exactly the fact the archive exists to preserve. Naming by data coverage would make the second
overwrite the first and destroy it.

**Parquet.** It is what FOCUS exports already use, `exports.py` can read it back, and it is about
a tenth the size of CSV: 12,776 rows is 209 KB. That matters for a file written every day.

Configured with `ARCHIVE_ACCOUNT` and `ARCHIVE_CONTAINER`; unset either to turn it off. The
app's managed identity needs **Storage Blob Data Contributor** on the account.

A failed archive never fails the refresh. By the time it runs the warehouse already holds new
data and the dashboard will show it, so turning an unreachable storage account into a failed
refresh would report a false problem and hide a real success. The outcome is recorded on the
ingest state and surfaced as its own toast instead.

`GET /api/archive` reports what the archive holds and whether it can be reached at all — the two
need different actions, since an empty archive means no refresh has run yet while an unreachable
one means no refresh ever will populate it. `POST /api/archive` writes today's copy from whatever
the warehouse currently holds, for the case where the refresh worked and the archive did not.

### Comparing two days — the History tab

Keeping the snapshots is only worth it if they can be read back, so `GET /api/archive/compare`
and the **History** tab do exactly that: two dated snapshots of the same dataset, joined and
differenced.

Both columns describe the *same period*. Azure revises cost data for several days after the
fact, so a difference is not spend that happened — it is spend that was always there and has
only now been counted. On this estate, one day apart, the June–August total moved by **$9.52
across 30 late-arriving usage records**, concentrated in Foundry Tools. That movement is
invisible from the dashboard, which only ever holds the latest read.

Both files are read with pyarrow rather than row by row in Python — they are columnar and
compressed, and the whole question is one join and one aggregate. A `FULL OUTER JOIN`, so a
resource that appears in one read and not the other is reported rather than silently dropped;
an inner join would hide exactly the movements worth looking at. The group-by column is checked
against a fixed list *before* anything is downloaded, since it is interpolated into SQL.

## Showing costs in another currency

The picker in the header converts every figure in the app. It defaults to **As billed**, which
shows exactly what Azure charged and is the only setting that needs no explanation.

Conversion pivots on Azure's own `CostInUsd` — the USD equivalent Microsoft records on the invoice
at the rate they actually applied — rather than re-deriving anything from the billed amount. Non-
USD targets then apply a published reference rate fetched daily from the ECB (via Frankfurter),
falling back to exchangerate-api for pegged currencies the ECB does not publish, such as AED. Set
`FX_RATES` to pin rates instead, for an organisation that books at a fixed internal rate — a live
rate is the *wrong* answer there. If no rate can be obtained the app shows the billed currency and
says why; a number converted at an unknown rate looks authoritative and cannot be reproduced.

There are two conversion paths, because there are two kinds of figure:

* **Warehouse-backed tabs convert in SQL.** `warehouse.connect(currency=…)` shadows the `costs`
  table with converted columns, so every query on that connection is already in the target
  currency. One place, rather than a rate multiplied into twenty different aggregates.
* **Azure-sourced tabs convert at the boundary.** Advisor savings, ESU list prices and the costs
  Resource Graph reports never touch the warehouse, so `currency.convert_money()` walks the
  response and multiplies only the keys on an explicit money allowlist. An allowlist rather than a
  heuristic: a response is full of numbers that must *not* move — counts, percentages, day windows,
  CPU readings — and "looks like money" is not a property a number has.

Thresholds move with the figures they judge. The anomaly detector's floors are money, so they are
scaled by the same rate; leaving a $15 floor at 15 while the data became rupees would flag a
different set of days depending on which currency you happened to be viewing. The estate did not
change, only the units — so the findings do not change either. Likewise a budget's limit is
converted alongside its spend, because a rupee figure measured against a dollar threshold would
draw a percentage that is simply false.

Creating a budget is different: Azure interprets its amount in the target
subscription's **native billing currency**, not the dashboard display currency.
The Tags form resolves subscriptions by ID, labels the native currency and asks
for an explicit amount. Unknown or mixed native currencies block submission
rather than guessing a unit or sending a converted suggestion unchanged.

The display currency is a *view*, never storage. The warehouse keeps what Azure billed, so a
converted figure can always be traced back and a change of rate does not rewrite history.

**Exports follow the screen.** A report ordered while the dashboard showed rupees contains rupees
— summary, areas, and the live datasets embedded in it — and the filename carries the code
(`cloudlens-cost-report-20260828-inr.xlsx`), because two files a week apart in different currencies
are otherwise indistinguishable in a downloads folder. The one deliberate exception is **Raw cost
rows**: those are the billed record, and rewriting them would make the export disagree with the
invoice it exists to reconcile against.

### Private storage

This deployment's tenant forces `publicNetworkAccess=Disabled` and `allowSharedKeyAccess=false`
on every storage account — ARM accepts `Enabled` and returns `Disabled`. So the archive account
is reached over a **private endpoint**:

```
vnet-cloudlens (10.70.0.0/16, centralindia)
  snet-app          10.70.1.0/24  delegated to Microsoft.Web/serverFarms  <- app VNet integration
  snet-postgres     10.70.2.0/24  delegated to the flexible server        <- PostgreSQL injection
  snet-privatelink  10.70.3.0/24  pe-cloudlens-blob -> st<20-random-hex-characters>
privatelink.blob.core.windows.net  linked to the VNet
```

Two settings make it work: regional VNet integration on the app, and `WEBSITE_DNS_SERVER`
=`168.63.129.16` so name resolution goes through Azure DNS and sees the private zone. The private
IP is RFC1918, so it routes through the VNet without `WEBSITE_VNET_ROUTE_ALL`.

Regional VNet integration needs Basic tier or above, which is why the plan is B1 rather than
Free. Cost Management exports use their own managed identities and storage-role assignments,
so this account can receive exports without enabling shared-key authentication.

## Settings

Things that change how the app gets its data, rather than what the data says.

### Scheduled exports — the fix for a slow refresh

A refresh through the Cost Details API asks Azure to *generate* a report and then waits. One
report is about forty seconds; a three-month load across three subscriptions is nine of them,
queued behind a per-subscription submission limit. That is minutes of waiting for data Azure
could have written to a blob overnight.

A scheduled export inverts it: Azure writes the data on its own timetable, and a refresh becomes
a blob read.

The thing that makes this possible in a governed tenant is **managed identity**. A Cost
Management export normally authenticates to its destination with a storage account key, and this
tenant forces `allowSharedKeyAccess=false` on every account — so the obvious approach fails with
"Key-based authentication is currently disabled on this storage account". Creating the export
with `identity: SystemAssigned` (API version `2023-08-01`) and granting *that* identity Storage
Blob Data Contributor sidesteps keys entirely. Verified working on 2026-08-28.

The Cost exports tab creates them, runs them on demand, and deletes them. One Azure limit shapes
what it offers:

* **One export covers one subscription.** Scheduling across an estate is one export each.

It used to record a second, that FOCUS could not be scheduled at subscription scope. That was
wrong. `2023-08-01` answers a FocusCost request with `Invalid definition type 'FocusCost'; valid
values: 'ActualCost, 'AmortizedCost'` — a 400 that names the type rather than the version, so it
read as a scope restriction. The identical body against `2025-03-01` creates the export and mints
its identity; Azure fills in the FOCUS `dataVersion` (`1.0r2`) itself. The old version also
*omits* FocusCost exports from its list, so ones created in the portal were invisible here.
New schedules are **FOCUS only**. Existing Actual, Amortized and external FOCUS schedules remain
visible as legacy/external; they are never automatically deleted, renamed or redirected.
Admins review a confirmation before creating/updating, running or deleting. Writes enforce
their permitted subscriptions and use delegated Azure credentials when configured; the Azure
caller still needs Cost Management Contributor and storage role-assignment privileges.
Cost exports discovers the accessible subscriptions and checks write access for each one,
including app-identity sign-ins. Users can configure one subscription or all selected writable
subscriptions; read-only entries stay visible but cannot be selected for changes. **Daily is
recommended**. Changing a subscription restores its own saved cadence or unsaved draft.

| Cadence | Dataset period |
|---|---|
| Daily | `MonthToDate`: cumulative current month, not one day |
| Weekly | `MonthToDate`: cumulative current month, not a rolling week |
| Monthly | `TheLastMonth`: previous calendar month; starts on the first of next month |

The manual first run uses the same period definition. Azure may restate even a closed month:
this is not a promise of invoice finality or automatic backfill of every historical month.
**Refresh latest**, available in Cost exports and the header refresh menu, requests a new
current-month report even for a Monthly schedule without changing that schedule. It waits for
a newer complete report before reporting success; an accepted Azure run is not a completed
database import. Azure billing delays still apply.
These are documented fields in the [2025-03-01 create/update API](https://learn.microsoft.com/en-us/rest/api/cost-management/exports/create-or-update?view=rest-cost-management-2025-03-01),
including Weekly and the monthly `TheLastMonth` example.

The destination is the deployment's existing `ARCHIVE_ACCOUNT_ID` with a **separate**
`FOCUS_EXPORT_CONTAINER` (default `focus-exports`) and `FOCUS_EXPORT_PREFIX` (default `focus`).
Each subscription gets `focus/<subscription-id>`; Azure appends export-name, period and run
folders. This is Azure Blob, not an SMB file share. Snapshot archives keep their old
`ARCHIVE_CONTAINER` and Focus/Actual/Amortized download mapping. Existing deployments must
provision the new container through their approved infrastructure workflow before scheduling;
the application does not create storage accounts or containers.

`CreateNewReport` retains every run. **No lifecycle deletion is added**: storage costs grow,
and any later manual retention policy must consider audit requirements. One stable export name
is derived from account/container/subscription prefix, not cadence: saving again updates the
same schedule, using Azure's GET `eTag`, rather than producing duplicates or changing another
deployment's export. Changing the destination configuration intentionally changes ownership.

Analytics loads one latest manifest per month within the selected export and dataset. It never
sums retained MTD runs with advancing end dates. Missing partitions, malformed manifests,
ambiguous definitions or count mismatches fail before atomic replacement; an incomplete newest
run leaves the warehouse unchanged rather than guessing or mixing it with an older run.
An automatic consumer checks this deployment's active owned exports every 15 minutes, independent
of Azure's delivery time. It creates no schedules and shares the foreground import slot. Set
`AUTO_FOCUS_SYNC=false` to pause that consumer independently of the daily Quick/history refresh.
Per-subscription export/import status and last successful ingestion survive restarts in the
existing metadata table. Reads are scoped to the signed-in user's permitted subscriptions.

Strict FOCUS ingestion compares complete usage-row multisets and changes only subscription/day
partitions that differ. Unchanged days remain untouched; legitimate duplicate usage, credits,
zero costs and Azure restatements are preserved. A complete explicitly empty report can remove
old charges only when its subscription and covered period are authoritative. Files are still
read and validated in full; this is delta **database reconciliation**, not a row-level Azure
delta feed. Rollups are refreshed even on replay to recover from a prior post-commit interruption.
Tag availability follows each resource's report evidence rather than a global latest-import flag.
The bounds are `EXPORT_MAX_BLOBS=10000`, `EXPORT_MAX_MANIFESTS=2000`, and
`EXPORT_MAX_FILES=60`. Exceeding a bound fails explicitly, never silently truncates; narrow the
prefix or deliberately raise the applicable cap after sizing memory/storage. Report reads
currently buffer selected files/rows, so this is not an unbounded-scale ingestion service.

### Exporting a selection

The daily archive covers the whole estate, because that is what makes two days comparable. The
other question — "give me just these subscriptions" — is answered here, with its own checkboxes
rather than the header's scope picker, since what you want to export is rarely what you happen to
be looking at.

CSV by default, in the same column order a FOCUS export produces, so the file is interchangeable
with one Azure wrote: `exports.py` can read it back, FinOps tooling ingests it, and it opens in
Excel. Parquet is offered too, at about a tenth the size. The result is written under
`selection/` with the date and a label, so it can never overwrite the daily archive.

The picker in the header narrows everything, and your choice sticks across reloads.

Scope is enforced rather than suggested. When a scope is active, the warehouse connection
shadows the `costs` table with a temp table containing only those subscriptions, so a query
physically cannot see anything else. The model forgetting a `WHERE` clause, or being told to
ignore the filter, still returns in-scope numbers. Live Azure tools get the same scope as an
explicit subscription list.

`test_scope.py` covers the escape routes (`costs.main.costs`, `WHERE 1=1 OR ...`, and friends);
`test_waste_scope.py` does the same for the live tools.

## Sharing it with a team

Sign-in is required for every page and every `/api/*` route. Two modes.

### SSO (recommended)

People sign in with their work account and see only the subscriptions their own Azure RBAC
allows. No accounts to create, no allow-list, no invitations.

```bash
az ad app create --display-name "CloudLens (SSO)" --sign-in-audience AzureADMyOrg
az ad sp create --id <appId>
az ad app update --id <appId> --is-fallback-public-client true \
  --public-client-redirect-uris "https://<your-host>/auth/callback"
```

```bash
AUTH_CLIENT_ID=<appId>
AUTH_TENANT_ID=<tenant the subscriptions live in>
AUTH_REDIRECT_URI=https://<your-host>/auth/callback
AUTH_ADMINS=you@yourcompany.com     # who may trigger an ingest
```

It's a public client with PKCE, so there's no secret to rotate. Set `AUTH_CLIENT_SECRET` if you'd
rather run it as a confidential client.

By default the app holds each person's own Azure token and calls ARM as them, so the list is
literally what Azure shows them and Azure itself refuses anything they aren't entitled to. That
needs `user_impersonation` on Azure Service Management, which most directories reserve to an
administrator — **until one approves it, nobody can sign in at all.**

That approval is a single click and the app guides you to it. The sign-in page offers
*"Approve CloudLens for this directory"* until a delegated sign-in has succeeded once, the
startup log prints the same link, and a refusal at the callback offers it again rather than
showing a raw OAuth error. Whoever deploys into the customer's tenant is normally the person who
can grant it.

`AUTH_DELEGATED_ARM=false` avoids the approval. The app asks Azure, as *itself*, which
subscriptions carry a **cost-capable** role assignment for the person — Owner, Contributor,
Reader, Cost Management Reader/Contributor, Billing Reader, or a custom role granting cost
actions, transitively so group access counts. Merely holding *some* role doesn't qualify: the
cost query afterwards runs as the app, so a VM Contributor would otherwise be handed access
Azure never granted. It needs only `openid profile offline_access`, which users can consent to
themselves.

Its limit is what the app's own identity can read. A subscription it has no Reader on is never
offered to anyone, so someone entitled to thirty may quietly be shown four. That limit is
reported rather than left to be discovered: `deploy.ps1 -CostScopeAllVisible` grants the
identity across everything the deployer can see and names anything left out, and `/api/me`
returns `access_mode` plus `app_visible_subscriptions` so the page can say whether a person
lacks a role or whether CloudLens simply cannot see their subscription.

Either way the entitlement is enforced the same everywhere: the picker, the header tiles, the
warehouse (physically shadowed), and any subscription named in a question that the person can't
see is dropped rather than honoured. Someone with no access gets an explicit 403 explaining
which role to ask for, never an empty page implying zero spend.

**Guests work.** If the subscriptions live in a different tenant from your colleagues' accounts,
point `AUTH_TENANT_ID` at the tenant that owns the subscriptions and add people there as guests.

### Local accounts

Used when `AUTH_CLIENT_ID` is unset, so the app still runs on a laptop with no tenant config.

```bash
python -m app.auth add priya            # prompts for a password
python -m app.auth add sam --admin      # may also trigger ingests
python -m app.auth list / passwd / remove
```

Accounts live in `data/users.json` (gitignored, PBKDF2-SHA256, never plaintext). The app watches
that file, so adding someone takes effect immediately and removing them ends their session.

A local account is the operator: it sees everything the server's own `az login` can see. That's
the difference from SSO, and why SSO is the right way to hand this to a team.

### Both modes

Anyone signed in can ask questions and read what they're entitled to. Only admins can start an
ingest, since that rewrites shared data.

Scripts use a bearer token instead of a session. Tokens run as the *server*, not as a person, so
they bypass RBAC — automation only:

```bash
export AUTH_API_TOKENS="ci:$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
curl -H "Authorization: Bearer $TOKEN" localhost:8100/api/warehouse
```

Cookies are `HttpOnly` and `SameSite=Lax` (which is why no CSRF token is needed). Five wrong
passwords lock that username and IP for a minute, doubling to fifteen. Wrong password and
unknown user return the same message and pay the same hashing cost, so neither text nor timing
enumerates accounts.

| Setting | Default | |
|---|---|---|
| `AUTH_CLIENT_ID`, `AUTH_TENANT_ID` | — | turn on Entra SSO |
| `AUTH_DELEGATED_ARM` | `true` | call Azure as the signed-in person; needs one-time admin consent. `false` resolves cost-capable roles as the app instead |
| `AUTH_ASK_EMAIL` | `true` | ask for an address first, and pass it to Entra as a login hint |
| `AUTH_REDIRECT_URI` | `http://localhost:8100/auth/callback` | must match the app registration |
| `AUTH_CLIENT_SECRET` | — | optional; makes it a confidential client |
| `AUTH_ADMINS` | — | emails that may trigger an ingest |
| `AUTH_ALLOW_LOCAL` | `false` | local accounts alongside SSO |
| `AUTH_RBAC_TTL_SECONDS` | `300` | how long a subscription list is cached |
| `AUTH_SESSION_HOURS` | `12` | how long a sign-in lasts |
| `AUTH_SECRET` | generated | set explicitly if you run more than one instance |
| `AUTH_USERS` | — | `name:<hash>[:admin],…` for deployments with no writable disk |
| `AUTH_API_TOKENS` | — | `name:token,…` for scripts |
| `AUTH_COOKIE_SECURE` | auto | force `Secure` behind upstream TLS |
| `AUTH_DISABLED` | `false` | switch auth off — localhost only |

Before putting it on a network: terminate TLS in front of it, set `AUTH_COOKIE_SECURE=true`, and
add the public redirect URI to the app registration. Framing is denied, the signed-in shell is
`no-store`, and `next=` redirects are restricted to paths on this site.

## Safety

- Read-only throughout. Nothing here can change an Azure resource.
- Model-written SQL is restricted to a single `SELECT`/`WITH`, and runs inside a PostgreSQL
  `READ ONLY` transaction — so a write that got past the word list is refused by the server
  itself rather than by a pattern match. File-reading functions (`pg_read_file`, `lo_import`)
  and schema-qualified table names that would sidestep the subscription scope are rejected too.
- Subscription scope is enforced by shadowing `costs` with a view containing only the rows the
  caller may see, so a forgotten `WHERE` clause cannot widen it.
- If a tool fails, the agent says so. It is explicitly forbidden from filling the gap with a
  plausible-looking table, which an earlier build of a sibling app did.
- The model is told today's date and the warehouse's loaded range. Without that it guesses — one
  test run labelled a correct July 2026 figure as "May 2024".
- If every subscription query fails, the code raises rather than reporting a confident `$0`.

## Refreshing

```bash
python ingest.py --months 6          # existing periods are replaced, not duplicated

# or from the running app (admin only)
curl -X POST -H "Authorization: Bearer $TOKEN" "localhost:8100/api/ingest?months=3"
```

`AUTO_INGEST=false` stops it ingesting on first start.

## Deploying to your own subscription

There are two ways in, and which you want depends on whether you are provisioning the
infrastructure as well as the app.

**`infra/deploy.ps1` — the default.** Bicep, one command, creates everything from nothing:
resource group, App Service, PostgreSQL, AI Foundry account and model deployment, managed
identity, and the Entra app registration for sign-in. Start here.

```powershell
./infra/deploy.ps1 -Location centralindia
```

See [infra/README.md](infra/README.md) for what it builds, what it costs, and how to point it
at other regions or subscriptions.

**`tools/deploy.ps1` — an existing subscription, no Bicep.** The older imperative
path. It deploys the app into a resource group it creates or reuses, points it at a PostgreSQL
server you already have rather than provisioning one, and does not create an app registration.
Useful for a single-instance deployment or where you are not allowed to run a subscription-scope
deployment; not the one to reach for otherwise.

```powershell
./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me `
  -ProjectEndpoint https://<resource>.services.ai.azure.com/api/projects/<project>
```

It creates the group, plan and app, assigns the managed identity, grants Reader and Cost
Management Reader on the subscription and a data-plane role on the Foundry account, pins
`AUTH_DATA_DIR` to `/home`, packages an explicit application-file allowlist, then polls until the
app is actually healthy rather than assuming the upload worked.

Four settings it will not let you get wrong, because each produces an app that looks deployed
and is not: one worker (sessions live in process memory), the account file outside
`wwwroot` (a zip deploy wipes it), a Foundry role (subscription Reader does not reach that data
plane, so the dashboard loads and every question 401s), and the managed identity's own access,
which is the ceiling for every user.

**`PROJECT_ENDPOINT` is required.** The app raises on startup without it, so you need an Azure AI
Foundry project and a model deployment even if you only want the dashboard.

### Three things that stop a personal subscription cold

None of these are the app, and all three are quicker to check than to debug.

**App Service quota is zero in most regions.** Credit-based subscriptions start with no compute
almost everywhere, and `az appservice list-locations` lists what the region *offers*, not what you
may create — so it will happily name a region that then refuses. Create a plan and see:

```powershell
az appservice plan create -g rg-cloudlens -n probe --is-linux --sku B1 -l <region>
```

**Model quota is per model.** `gpt-4.1` is often zero while `gpt-4.1-mini` has plenty. Check
before deploying, and pass whatever you find to `-ModelName`:

```powershell
az cognitiveservices usage list -l <region> -o json |
  ConvertFrom-Json | Where-Object limit -gt 0 | Select-Object @{n='q';e={$_.name.value}}, limit
```

Avoid the reasoning models here: the agent sends `temperature=0.1`, which they reject.

**WebDirect subscriptions cannot use the Cost Details API.** Visual Studio, pay-as-you-go and MSDN
are all refused with a 422, so **Refresh data** cannot load a warehouse on them. The live
dashboard figures and the agent work normally; for loaded data, point it at a Cost Management
export and choose **FOCUS report**.

### Signing in with an Azure account

Set two things and the local password disappears:

```powershell
./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me -ProjectEndpoint https://... `
  -EntraClientId <app-id> -EntraTenantId <tenant-id> -Admins you@example.com -DelegatedArm
```

Register an app in the tenant that owns the subscription, add
`https://<app>.azurewebsites.net/auth/callback` as a redirect URI under **Mobile and desktop
applications** — *not* Web — and pass its ids above. CloudLens redeems the code with PKCE and
never uses a secret, and Entra only permits that for a public client: registering the callback
under Web instead fails at sign-in with `AADSTS7000218`. This is also why a tenant that forbids
app secrets outright, as many do, is no obstacle.

The authority is `login.microsoftonline.com/<tenant>` — a single tenant, so people outside that
directory cannot sign in.

### Letting anyone with an Azure account sign in

`-MultiTenant` swaps that single tenant for `organizations`, so a work or school account from
**any** directory can sign in and see whatever their own Azure RBAC allows — across every tenant
they belong to. Their account does not need to exist in yours.

```powershell
./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me -ProjectEndpoint https://... `
  -EntraClientId <app-id> -MultiTenant -Admins you@example.com
```

The registration has to allow it:

```powershell
az ad app update --id <app-id> --sign-in-audience AzureADMultipleOrgs
```

`organizations`, not `common`: personal Microsoft accounts cannot hold Azure RBAC, so admitting
them produces a successful sign-in and an empty dashboard, which is worse than a clear refusal.

This implies `-DelegatedArm`, and the deploy refuses the combination without it. Resolving access
*as the app* means reading role assignments, which only works inside the app's own directory —
so without a delegated token every external user would sign in and see nothing, with the app
looking perfectly healthy.

**One token per tenant.** An ARM token is issued by one directory and refused by every other.
After sign-in the app lists the tenants you can reach, gets a token for each, and records which
subscription each token can read. A directory that refuses — no consent there, or Conditional
Access — is reported rather than silently dropped.

**No Azure access is an answer, not an error.** Someone who signs in with no subscriptions gets
an empty dashboard that says so. Nothing is hidden and nothing pretends to have failed.

### When a tenant requires admin approval

Many organisations disable user consent. Sign-in then stops with `AADSTS90094` — an Entra policy,
not something an application can work around. CloudLens shows the reason and a link to
`/auth/admin-consent`, which starts the tenant-wide consent flow. One administrator of that
directory approves it once and everyone there can sign in afterwards, each with their own access.

**What each person sees is their own Azure RBAC**, resolved one of two ways:

- **Default (`AUTH_DELEGATED_ARM=true`).** The app uses the signed-in person's own ARM token, so
  Azure refuses rather than the app filtering. Needs one-time admin consent for Azure Service
  Management `user_impersonation`; in your own tenant you are the admin.
- **App identity.** The app asks ARM, as itself, which subscriptions carry a cost-capable role
  assignment for that person's object id. Transitive, so group-granted access counts. No consent
  needed, but bounded by the subscriptions the app's identity can read.

`-Admins` lists who may run a refresh. Everyone else can read and ask, which is the common case.

## Deploying by hand, and what it teaches

You should not need this — `infra/deploy.ps1` does all of it — but the constraints below are
the ones that produce an app that looks deployed and is not, so they are worth knowing whatever
route you took.

App Service Linux, Python 3.13, single instance:

```bash
RG=rg-cloudlens; APP=cloudlens-me; LOC=centralindia
az appservice plan create -g $RG -n asp-cloudlens-b1 --is-linux --sku B1 -l $LOC
az webapp create -g $RG -p asp-cloudlens-b1 -n $APP --runtime "PYTHON:3.13"
az webapp config set -g $RG -n $APP --always-on true \
  --startup-file "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
az webapp deploy -g $RG -n $APP --src-path deploy.zip --type zip
```

**B1, not Free.** Regional VNet integration — which the archive's private endpoint depends on —
needs Basic or above. Always On comes with it, so the app stops cold-starting.

**Deploy with `--type zip`, never `--type static`.** A static deploy targeting a single file
replaces the whole `wwwroot`, discarding the Oryx build output; the app then fails to start with
`No module named uvicorn` and takes ~20 minutes to restore.

**One worker, one instance.** Sessions and the header snapshot live in process memory, so a
second worker would send a signed-in person to a process that's never heard of them. Scaling out
means moving sessions to Redis first.

**It runs as a managed identity, not as you.** There's no `az login` on App Service, so
`DefaultAzureCredential` falls through to the identity:

```bash
MI=$(az webapp identity assign -g $RG -n $APP --query principalId -o tsv)
for SUB in <subscription-ids>; do
  az role assignment create --assignee-object-id $MI --assignee-principal-type ServicePrincipal \
    --role "Reader" --scope "/subscriptions/$SUB"
  az role assignment create --assignee-object-id $MI --assignee-principal-type ServicePrincipal \
    --role "Cost Management Reader" --scope "/subscriptions/$SUB"
done
az role assignment create --assignee-object-id $MI --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope <ai-resource-id>
```

A subscription the identity wasn't granted is invisible to everyone, however much access they
personally have. Worth checking after any deployment.

**Point the app at its database.** The warehouse lives on PostgreSQL, not on the app's disk, so
there is nothing to seed or to lose to a zip deploy — but the app refuses to start without
`PGHOST`, deliberately, because there is no correct guess for where a customer's cost data lives:

```bash
az webapp config appsettings set -g $RG -n $APP --settings \
  PGHOST=$PG_SERVER.postgres.database.azure.com \
  PGDATABASE=cloudlens \
  PGUSER=$IDENTITY_NAME \
  PGSSLMODE=require
```

No password: the app presents an Entra token from its managed identity, so there is no database
credential in app settings to rotate or leak. The identity must already exist as a role on the
server — that is a data-plane operation, done on the server rather than through ARM.

`AUTH_DATA_DIR` still has to point at `/home`: a zip deploy replaces `/home/site/wwwroot`, so the
account file at a default path is deleted by the next deploy and everyone is signed out.

Configuration goes in app settings, not `.env` — the deployment package excludes `.env`,
`data/users.json` and `data/.session_secret`. Publish accounts as `AUTH_USERS` with a PBKDF2 hash
(`python -m app.auth hash`), and set `AUTH_SECRET` explicitly so sessions survive a restart.

## The `costs` table

| Column | Notes |
|---|---|
| `ChargePeriodStart` | DATE — usage date |
| `BilledCost`, `CostInUsd` | DOUBLE |
| `BillingCurrency` | |
| `SubAccountId`, `SubAccountName` | subscription |
| `ResourceGroup`, `ResourceId`, `ResourceName` | `ResourceName` is derived from the id |
| `ServiceName`, `ServiceFamily`, `ServiceSubcategory`, `MeterName`, `ProductName` | |
| `RegionName`, `PricingQuantity`, `UnitOfMeasure`, `UnitPrice` | |
| `ChargeCategory`, `PricingModel`, `BenefitName`, `PublisherName`, `CostCenter`, `Tags` | |

Column names are mapped from whatever the export actually uses — they differ between EA, MCA and
pay-as-you-go — so downstream code sees one stable schema. Verified against a real MCA export
with 65 source columns.

## Endpoints

Everything under `/api/` needs a session cookie or bearer token. A browser without one is
redirected to `/login`.

| | |
|---|---|
| `GET /api/health` | model, subscription count |
| `GET /api/subscriptions` | subscriptions in the picker, with spend |
| `GET /api/overview[?scope=id,id]` | header figures |
| `GET /api/warehouse` | row count, date range, ingest progress |
| `POST /api/ingest?months=N` | refresh the warehouse — admin only |
| `GET /api/exports` | Cost Management exports visible to you |
| `POST /api/exports/ingest` | load an export — admin only |
| `GET /api/archive` | what the daily archive holds, and whether it is reachable |
| `POST /api/archive?metric=` | write today's archive from the current warehouse — admin only |
| `GET /api/archive/compare` | what Azure restated between two archived days |
| `POST /api/archive/export` | export chosen subscriptions as CSV or Parquet |
| `GET /api/schedules` | scheduled Cost Management exports and where they write |
| `POST /api/schedules` | create a scheduled export with a managed identity — admin only |
| `POST /api/schedules/run` | produce a scheduled export now — admin only |
| `DELETE /api/schedules` | remove a schedule — admin only |
| `GET /api/budgets[?scope=id,id]` | budgets with percent used, headroom and status |
| `POST /api/budgets` | create a tag-filtered budget — needs Cost Management Contributor |
| `POST /api/ask` | streamed answer (SSE) |
| `GET /api/dashboard/executive` | the opening view, in one warehouse pass |
| `GET /api/dashboard/billing[?month=YYYY-MM]` | prior completed calendar-month statement by default, with price/basis coverage and reconciled breakdowns |
| `GET /api/dashboard/billing/opportunities` | current compatible savings estimates and scoped commitment lifecycle watch; `refresh=true` bypasses the snapshot cache |
| `GET /api/dashboard/sections` | the navigation rail and what each area costs |
| `GET /api/dashboard/{section}` | one tab: KPIs, trend, services, regions, top resources |
| `GET /api/dashboard/savings` | completed Savings results with freshness; stale results revalidate in the background |
| `GET /api/dashboard/savings/progress` | immediate cached/partial Savings and per-source progress; `refresh=true` only on the first explicit refresh request |
| `GET /api/dashboard/waste` | idle and orphaned resources (live) |
| `GET /api/dashboard/rightsizing` | per-VM CPU with power state and cost (live) |
| `GET /api/dashboard/esu` | end-of-support inventory and ESU cost (live) |
| `GET /api/dashboard/advisor` | Advisor recommendations (live) |
| `GET /api/dashboard/commitments` | reservation, savings plan and Spot coverage, month by month |
| `GET /api/dashboard/governance` | tag coverage and accelerated networking (live) |
| `GET /api/dashboard/rates` | reservation and savings plan opportunity, from Advisor (live) |
| `GET /api/dashboard/health` | retirements and required migrations, from Service Health (live) |
| `GET /api/dashboard/azure-updates` | official product announcements with freshness, categories and coverage |
| `GET /api/dashboard/azure-status` | public incident RSS; empty is not a global health guarantee |
| `GET /api/dashboard/service-health?scope=id,id` | private Service Health for explicit permitted subscriptions; all retained event types/statuses |
| `GET /api/dashboard/change-analysis?scope=id,id&window_days=7` | read-only recent changes and billed-cost context; explicit permitted GUID scopes, 1-14 days |
| `GET /api/dashboard/performance?scope=id,id&window_days=7` | bounded service-specific metric observations and native-currency billed-cost context |
| `GET /api/costs/raw.csv` | the underlying cost rows for the selected subscriptions |
| `GET /api/report/{xlsx,pptx,csv,md,json}` | a report as a plain link; `probe=1` validates without building |
| `GET /api/report/options` | what this person could include |
| `POST /api/report` | the same report from a JSON selection |
| `POST /api/auth/login` · `/logout` | sign in and out |
| `GET /auth/login` → `/auth/callback` | Entra sign-in round trip |
| `GET /api/auth/me` | who's signed in and how much they can see |
| `GET /healthz` | liveness for a load balancer |
| `GET /readyz` | public, bounded PostgreSQL/core-schema readiness; 200 ready or 503 not ready |

`/healthz` reports process liveness only. `/readyz` is disabled during startup/shutdown
and probes core table/column readability on a fresh, read-only PostgreSQL connection.
It creates or migrates nothing and selects no customer rows. Concurrent callers share one
probe; responses have a five-second deadline and results are cached for five seconds.
The response contains only a status, with no database, tenant or error details.
Deployment scripts require readiness and the existing authentication challenge. This does
not certify model availability, Azure permissions, ingest completion or data freshness.

## Tests

```powershell
pip install -r requirements.txt -r requirements-dev.txt

# Database-free checks
python -m pytest -m "not postgres and not network and not azure"

# Database-backed checks: use a dedicated, empty database on a development server.
$env:CLOUDLENS_PG_TEST = "<development-server>.postgres.database.azure.com"
$env:CLOUDLENS_PG_DB = "<empty-test-database>"
$env:CLOUDLENS_PG_USER = "<test-role>"
$env:PGPASSWORD = (az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)
python -m pytest -m postgres
```

Selected PostgreSQL tests require explicit connection settings; they never fall back to
an embedded database or silently skip missing prerequisites. Each fixture uses a disposable
schema. Do not point the suite at production or a database containing application data:
schema initialization can migrate legacy tables, so the fixtures reject shared tables
and views before starting.

New tests should use ordinary pytest functions and mark database requirements with
`pytest.mark.postgres`. Legacy scripts run in isolated subprocesses; truly offline scripts
opt in with `# cloudlens-test: requires-offline`. Tests that call Azure remain disabled
unless `CLOUDLENS_AZURE_TEST=1` is set.

[tests/README.md](tests/README.md) has the detail: the markers, the seeded fixture data, and
every optional environment variable.

## Working on CloudLens

| Layer | Main files | Responsibility |
| --- | --- | --- |
| HTTP and lifecycle | `app/main.py` | Request handling, service composition, startup and shutdown |
| Identity and scope | `app/auth.py`, `app/entra.py`, `app/scopes.py` | Sessions, Microsoft sign-in and permitted subscriptions |
| Cost domain | `app/billing.py`, `app/reporting.py`, `app/resources.py`, other feature modules | Financial rules and feature-specific reads |
| Persistence | `app/warehouse.py`, `app/store.py` | Scoped queries, schema management and PostgreSQL connections |
| Dashboard shell | `web/assets/dashboard.js` | Navigation, request context, cached views and shared table rendering |
| Feature views | `web/assets/dashboard-*.js` | Billing, resources, savings, monitoring and reporting controllers |
| Frontend state and calculations | `dashboard-state.js`, `dashboard-table-model.js`, `cost-totals.js` | Interaction state, row identity/search/order and currency-safe totals |
| Deployment | `infra/`, `tools/package.ps1` | Infrastructure provisioning and source-only release packaging |
| Tests | `tests/`, `conftest.py` | Unit/browser checks and isolated PostgreSQL fixtures |

Keep domain calculations independent of HTTP and DOM state. Feature views receive an
explicit context from the shell; they must not import the shell back. New tests should
exercise behavior rather than depend on exact comment text or source formatting.

```bash
ruff check .        # lint; CI runs exactly this
pytest -n 4         # tests; CI runs these on Linux and Windows, 3.11 and 3.13
```

Tooling configuration lives in `pyproject.toml`. Use `.env.example` for supported local
application settings. Keep `.env`, local account data, passwords and tokens out of Git.
Azure deployments can use managed identity for Azure and PostgreSQL access; local
development credentials and optional authentication modes still require secret handling.

## Subscription scope
