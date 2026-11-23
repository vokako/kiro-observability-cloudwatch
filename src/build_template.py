#!/usr/bin/env python3
"""Generate the CloudFormation template for Kiro observability on Amazon CloudWatch.

    python3 src/build_template.py template    > template/kiro-observability.yaml
    python3 src/build_template.py dashboard <region> <log-group>    # dashboard body JSON only

The template is generated so that the dashboard JSON and the inline Lambda code stay valid
and readable here instead of being hand-escaped inside YAML.

Findings from testing against real data that shaped this file:
- CloudWatch PromQL limits (end - start) plus the range selector to 31 days. Daily-metric widgets
  therefore use [1d] windows with a 1-day step; period totals come from the real-time log widgets.
- Real-time event attributes are nested in CloudWatch Logs: attributes.kiro.*.
- Kiro accepts user_activity_report.otel_temporality only in lower case (delta | cumulative).
- IAM rejects resource ARNs for the Kiro Enterprise Management preview actions
  ("The policy failed legacy parsing"), so that role is scoped by action with Resource "*".
- The cfnresponse module is not available in the python3.13 runtime; responses are sent directly.
- A chart widget's plotOptions requires a view-specific style object; none is sent.
"""
import json
import sys

ROTATE_EVERY_DAYS = 30   # Secrets Manager rotates each API key this often ...
KEY_LIFETIME_DAYS = 90   # ... and each key expires this long after it is created.
# The previous key stays valid until the next rotation, so a request that Kiro signed just before a
# rotation still succeeds. IAM allows at most two keys per user and service, which this cycle respects.

M = 'filter eventName = "kiro.chat.message"'
T = 'filter eventName = "kiro.tool.call"'


# ----------------------------------------------------------------------------------- dashboard
def dashboard(region="${AWS::Region}", lg="${LogGroupResolved}"):
    def log(title, q, view, x, y, w, h):
        return {"type": "log", "x": x, "y": y, "width": w, "height": h,
                "properties": {"region": region, "title": title, "view": view,
                               "query": "SOURCE '%s' | %s" % (lg, "\n| ".join(q))}}

    def prom(title, q, view, x, y, w, h):
        return {"type": "chart", "x": x, "y": y, "width": w, "height": h,
                "properties": {"view": view, "title": title, "region": region,
                               "data": {"queries": [{"id": "a", "type": "cloudwatch-metrics",
                                                     "language": "PromQL", "query": q, "step": 86400}]}}}

    def text(md, y, h):
        return {"type": "text", "x": 0, "y": y, "width": 24, "height": h, "properties": {"markdown": md}}

    def day(metric):  # one day's total per series
        return 'sum_over_time({"%s"}[1d])' % metric

    w = [
        text("# Kiro usage\n"
             "**Top: real-time events** from log group `%s`. They arrive seconds after each request, and the totals "
             "follow the dashboard time range. **Bottom: daily metrics** `kiro.daily.*`, one point per user per day, "
             "sent at 00:30 UTC for the previous day. Both start on the day the export was turned on; "
             "for earlier usage, use the CSV report in Amazon S3." % lg, 0, 2),

        log("Totals (selected time range)",
            [M, "stats sum(attributes.kiro.credits.used) as credits, sum(attributes.kiro.credits.overage) as overage_credits, "
                "count(*) as requests, count_distinct(attributes.kiro.user.id) as active_users, "
                "count_distinct(attributes.kiro.root_conversation_id) as sessions, "
                "sum(attributes.kiro.tool_call_count) as tool_calls_requested"], "table", 0, 2, 24, 3),

        log("Usage by user",
            [M, "stats sum(attributes.kiro.credits.used) as credits, sum(attributes.kiro.credits.overage) as overage, "
                "count(*) as requests, count_distinct(attributes.kiro.root_conversation_id) as sessions, "
                "max(attributes.kiro.usage.limit) as usage_limit by attributes.kiro.user.email, attributes.kiro.subscription.tier",
             "sort credits desc", "limit 100"], "table", 0, 5, 12, 7),
        log("Credits per day by user",
            [M, "stats sum(attributes.kiro.credits.used) as credits by bin(1d), attributes.kiro.user.email"],
            "timeSeries", 12, 5, 12, 7),

        log("Credits by model", [M, "stats sum(attributes.kiro.credits.used) as credits by attributes.kiro.model.name"],
            "pie", 0, 12, 8, 6),
        log("Response time by model (ms)",
            [M, "stats count(*) as requests, pct(attributes.kiro.response.duration_ms, 50) as p50_ms, "
                "pct(attributes.kiro.response.duration_ms, 95) as p95_ms, "
                "sum(attributes.kiro.answer.cut_off) as cut_off by attributes.kiro.model.name",
             "sort requests desc"], "table", 8, 12, 8, 6),
        log("Clients and versions",
            [M, "stats count(*) as requests, count_distinct(attributes.kiro.user.id) as users, "
                "sum(attributes.kiro.credits.used) as credits by attributes.kiro.client.type, attributes.kiro.client.version",
             "sort requests desc"], "table", 16, 12, 8, 6),

        log("Credits per hour", [M, "stats sum(attributes.kiro.credits.used) as credits by bin(1h)"],
            "timeSeries", 0, 18, 12, 6),
        log("Requests per hour by client", [M, "stats count(*) as requests by bin(1h), attributes.kiro.client.type"],
            "timeSeries", 12, 18, 12, 6),

        log("Most expensive sessions",
            [M, "stats sum(attributes.kiro.credits.used) as credits, count(*) as requests, "
                "sum(attributes.kiro.tool_call_count) as tool_calls, min(@timestamp) as started "
                "by attributes.kiro.root_conversation_id, attributes.kiro.user.email",
             "sort credits desc", "limit 20"], "table", 0, 24, 12, 7),
        log("Agent mode and sub-agents",
            [M, "stats count(*) as requests, sum(attributes.kiro.credits.used) as credits "
                "by attributes.kiro.agent_mode, attributes.kiro.request.subagent"], "table", 12, 24, 6, 7),
        log("Sessions by number of requests",
            [M, "stats count(*) as requests by attributes.kiro.root_conversation_id",
             "stats count(*) as sessions by ceil(requests / 10) * 10 as requests_bucket",
             "sort requests_bucket asc"], "bar", 18, 24, 6, 7),

        log("Tool calls (reported by the client)",
            [T, "stats count(*) as calls, avg(attributes.kiro.tool.duration_ms) as avg_ms "
                "by attributes.kiro.tool.name, attributes.kiro.tool.kind, attributes.kiro.tool.outcome",
             "sort calls desc", "limit 50"], "table", 0, 31, 12, 6),
        log("MCP servers and errors",
            [T, "filter attributes.kiro.tool.kind = 'MCP' or ispresent(attributes.kiro.error.type)",
             "stats count(*) as calls by attributes.kiro.mcp.server, attributes.kiro.tool.outcome, attributes.kiro.error.type",
             "sort calls desc"], "table", 12, 31, 12, 6),

        log("Latest requests",
            [M, "fields @timestamp, attributes.kiro.user.email as user, attributes.kiro.model.name as model, "
                "attributes.kiro.client.type as client, attributes.kiro.agent_mode as mode, "
                "attributes.kiro.credits.used as credits, attributes.kiro.response.duration_ms as ms, "
                "attributes.kiro.tool_call_count as tools",
             "sort @timestamp desc", "limit 100"], "table", 0, 37, 24, 6),

        text("## Daily metrics (PromQL)\nEach point is one day's total, the same data as the CSV report.", 43, 1),
        prom("Credits, latest day", "sum(%s)" % day("kiro.daily.credits"), "number", 0, 44, 4, 4),
        prom("Overage credits, latest day", "sum(%s)" % day("kiro.daily.overage_credits"), "number", 4, 44, 4, 4),
        prom("Messages, latest day", "sum(%s)" % day("kiro.daily.messages"), "number", 8, 44, 4, 4),
        prom("Conversations, latest day", "sum(%s)" % day("kiro.daily.conversations"), "number", 12, 44, 4, 4),
        prom("Active users, latest day", 'count(sum by ("kiro.user.id") (%s))' % day("kiro.daily.credits"), "number", 16, 44, 4, 4),
        prom("Requests with tools, latest day", "sum(%s)" % day("kiro.daily.requests_with_tools"), "number", 20, 44, 4, 4),
        prom("Daily credits by user", 'sum by ("kiro.user.email") (%s)' % day("kiro.daily.credits"), "column", 0, 48, 12, 6),
        prom("Daily credits by model", 'sum by ("kiro.model.name") (%s)' % day("kiro.daily.model_credits"), "column", 12, 48, 12, 6),
        prom("Daily messages by client", 'sum by ("kiro.client.type") (%s)' % day("kiro.daily.messages"), "column", 0, 54, 8, 6),
        prom("Average response time by user (ms)",
             'avg by ("kiro.user.email") (avg_over_time({"kiro.daily.avg_response_ms"}[1d]))', "line", 8, 54, 8, 6),
        prom("Client versions in use (users)",
             'count by ("kiro.client.version") (max_over_time({"kiro.daily.client_version_seen"}[1d]))', "column", 16, 54, 8, 6),
        prom("Top 10 tools per day (requested by the model)",
             'topk(10, sum by ("kiro.tool.name") (%s))' % day("kiro.daily.tool_calls"), "column", 0, 60, 24, 6),
    ]
    return {"start": "-P30D", "periodOverride": "inherit", "widgets": w}


QUERIES = [
    ("Credits by user", [M, "stats sum(attributes.kiro.credits.used) as credits, count(*) as requests by attributes.kiro.user.email",
                         "sort credits desc"]),
    ("Credits by model", [M, "stats sum(attributes.kiro.credits.used) as credits by attributes.kiro.model.name"]),
    ("Top sessions", [M, "stats sum(attributes.kiro.credits.used) as credits, count(*) as requests "
                         "by attributes.kiro.root_conversation_id, attributes.kiro.user.email", "sort credits desc", "limit 20"]),
    ("Tool calls by outcome", [T, "stats count(*) as calls by attributes.kiro.tool.name, attributes.kiro.tool.outcome",
                               "sort calls desc"]),
    ("Latest events", ["fields @timestamp, eventName, attributes.kiro.user.email, attributes.kiro.model.name, "
                       "attributes.kiro.credits.used", "sort @timestamp desc", "limit 50"]),
]

# ------------------------------------------------------------------- Lambda: API keys + rotation
# One function serves two callers:
#  - Secrets Manager rotation (event has "Step"): createSecret / setSecret / testSecret / finishSecret.
#  - CloudFormation custom resource KeyCleanup (event has "RequestType"): on Delete, removes every API key
#    of the two ingest users so CloudFormation can delete the users.
# The key is never logged or returned.
KEY_LAMBDA = r'''
import json, os, time, urllib.request, urllib.error, boto3
iam = boto3.client("iam")
sm = boto3.client("secretsmanager")
CFG = json.loads(os.environ["CFG"])          # {secret ARN: {user, service, endpoint, path, headers}}
LIFETIME = int(os.environ["KEY_LIFETIME_DAYS"])
CUR, PEND = "kiro-otel:current-credential", "kiro-otel:pending-credential"


def conf(sid):
    if sid in CFG:
        return sid, CFG[sid]
    arn = sm.describe_secret(SecretId=sid)["ARN"]
    return arn, CFG[arn]


def keys(c):
    r = iam.list_service_specific_credentials(UserName=c["user"], ServiceName=c["service"])
    return sorted(r.get("ServiceSpecificCredentials", []), key=lambda k: k["CreateDate"])


def tag(sid, name):
    return {t["Key"]: t["Value"] for t in sm.describe_secret(SecretId=sid).get("Tags", [])}.get(name)


def create(sid, token):
    try:
        sm.get_secret_value(SecretId=sid, VersionId=token, VersionStage="AWSPENDING")
        return
    except sm.exceptions.ResourceNotFoundException:
        pass
    arn, c = conf(sid)
    ks = keys(c)
    if len(ks) >= 2:  # IAM allows two keys: keep the one Kiro uses now, delete the rest
        ids = [k["ServiceSpecificCredentialId"] for k in ks]
        keep = tag(sid, CUR)
        keep = keep if keep in ids else ids[-1]
        for i in ids:
            if i != keep:
                iam.delete_service_specific_credential(UserName=c["user"], ServiceSpecificCredentialId=i)
    k = iam.create_service_specific_credential(UserName=c["user"], ServiceName=c["service"],
                                               CredentialAgeDays=LIFETIME)["ServiceSpecificCredential"]
    value = {"OTEL_EXPORTER_OTLP_ENDPOINT": c["endpoint"],
             "OTEL_EXPORTER_OTLP_HEADERS": c["headers"].replace("__TOKEN__", k["ServiceCredentialSecret"])}
    sm.put_secret_value(SecretId=sid, ClientRequestToken=token, SecretString=json.dumps(value),
                        VersionStages=["AWSPENDING"])
    sm.tag_resource(SecretId=sid, Tags=[{"Key": PEND, "Value": k["ServiceSpecificCredentialId"]}])


def test(sid, token):
    arn, c = conf(sid)
    v = json.loads(sm.get_secret_value(SecretId=sid, VersionId=token, VersionStage="AWSPENDING")["SecretString"])
    h = dict(p.split("=", 1) for p in v["OTEL_EXPORTER_OTLP_HEADERS"].split(","))
    h["Content-Type"] = "application/json"
    body = b'{"resourceLogs":[]}' if c["path"] == "/v1/logs" else b'{"resourceMetrics":[]}'
    url = v["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/")
    url = url if url.endswith(c["path"]) else url + c["path"]
    err = None
    for _ in range(12):  # a new key can take a few seconds to work
        try:
            if urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h, method="POST"),
                                      timeout=15).status == 200:
                return
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
        time.sleep(5)
    raise Exception("new key was not accepted by %s: %s" % (url, err))


def finish(sid, token):
    vs = sm.describe_secret(SecretId=sid)["VersionIdsToStages"]
    cur = next((v for v, s in vs.items() if "AWSCURRENT" in s), None)
    if cur != token:
        sm.update_secret_version_stage(SecretId=sid, VersionStage="AWSCURRENT",
                                       MoveToVersionId=token, RemoveFromVersionId=cur)
    pend = tag(sid, PEND)
    if pend:
        sm.tag_resource(SecretId=sid, Tags=[{"Key": CUR, "Value": pend}])
    # The previous key is kept until the next rotation, so requests signed with it still succeed.


def send(ev, cx, status, reason=""):
    b = json.dumps({"Status": status, "PhysicalResourceId": ev.get("PhysicalResourceId", "key-cleanup"),
                    "Reason": reason or cx.log_stream_name, "StackId": ev["StackId"],
                    "RequestId": ev["RequestId"], "LogicalResourceId": ev["LogicalResourceId"], "Data": {}}).encode()
    urllib.request.urlopen(urllib.request.Request(ev["ResponseURL"], data=b, method="PUT",
                                                  headers={"Content-Type": ""}), timeout=30)


def handler(ev, cx):
    if "Step" in ev:  # Secrets Manager rotation
        sid, token, step = ev["SecretId"], ev["ClientRequestToken"], ev["Step"]
        print("rotation", step, sid)
        {"createSecret": create, "testSecret": test, "finishSecret": finish}.get(step, lambda s, t: None)(sid, token)
        return
    try:  # CloudFormation custom resource
        if ev["RequestType"] == "Delete" and ev["ResourceType"] == "Custom::KeyCleanup":
            for c in CFG.values():
                for k in keys(c):
                    iam.delete_service_specific_credential(UserName=c["user"],
                                                           ServiceSpecificCredentialId=k["ServiceSpecificCredentialId"])
        send(ev, cx, "SUCCESS")
    except Exception as e:
        print(e)
        send(ev, cx, "FAILED", str(e)[:500])
'''.strip("\n")

# ------------------------------------------------------------------- Lambda: Kiro Controls policy
# Merges ONLY the observability keys into the existing KGL file of the Controls policy, validates and publishes.
# Rules, other settings, comments and YAML tags stay byte-for-byte.
KIRO_LAMBDA = r'''
import json,os,re,time,urllib.request,urllib.error,botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
R=os.environ.get("AWS_REGION","us-east-1");EP="https://management.%s.kiro.dev/"%R
S=re.compile(r"(?m)^settings\s*:\s*(#.*)?$")
def em(op,**b):
 c=botocore.session.get_session().get_credentials().get_frozen_credentials();d=json.dumps(b).encode()
 q=AWSRequest("POST",EP,data=d,headers={"Content-Type":"application/x-amz-json-1.0","X-Amz-Target":"KiroControlPlaneQDeveloperService."+op})
 SigV4Auth(c,"q",R).add_auth(q)
 try:return json.loads(urllib.request.urlopen(urllib.request.Request(EP,data=d,headers=dict(q.headers),method="POST"),timeout=30).read() or b"{}")
 except urllib.error.HTTPError as e:
  t=e.read().decode()[:400]
  if "ResourceNotFound" in t:return None
  raise Exception("%s HTTP %s: %s"%(op,e.code,t))
def uq(v):
 v=v.split(" #")[0].strip();return v[1:-1] if len(v)>1 and v[0]==v[-1] and v[0] in "\"'" else v
def merge(t,kv):
 L=t.splitlines();i=next((n for n,l in enumerate(L) if S.match(l)),None)
 if i is None:
  if any(l.startswith("settings") for l in L):raise Exception("inline settings: edit the policy by hand")
  L+=["","settings:"];i=len(L)-1
 e=next((n for n in range(i+1,len(L)) if L[n] and L[n][0] not in " \t#"),len(L))
 m=next((re.match(r"\s+",L[n]).group() for n in range(i+1,e) if re.match(r"\s+\S+\s*:",L[n])),"  ")
 j=max([n for n in range(i+1,e) if L[n].strip()]+[i])+1
 for k,v in kv.items():
  h=next((n for n in range(i+1,e) if re.match(r"\s+"+re.escape(k)+r"\s*:",L[n])),None)
  if h is None:L.insert(j,m+k+": "+v);j+=1;e+=1
  elif uq(L[h].split(":",1)[1])!=uq(v):L[h]=m+k+": "+v
 return "\n".join(L)+"\n"
def pick(p):
 if p.get("PolicyId"):return p["PolicyId"]
 rs,t=[],None
 while True:
  a=em("ListEnterpriseManagementResources",**({"nextToken":t} if t else {}));rs+=a.get("resources",[]);t=a.get("nextToken")
  if not t:break
 h=[r["resourceId"] for r in rs if any(s["scopeType"]=="PROFILE" and p.get("ProfileId","") in ("",s["scopeId"]) for s in r.get("associatedScopes",[]))]
 if len(h)!=1:raise Exception("%d policies assigned to a profile; set KiroPolicyId. Found: %s"%(len(h),[(r["resourceId"],r.get("name")) for r in rs]))
 return h[0]
def apply(p,kv,publish=True,guard=None):
 rid=pick(p);dr=em("GetEnterpriseManagementResource",resourceId=rid,revision="DRAFT")
 dirty=bool(dr and dr.get("updatedAt"))
 if dirty and p.get("AllowExistingDraft")!="true":raise Exception("policy %s has unpublished draft changes; publish or discard them, or set AllowExistingDraft=true"%rid)
 rev="DRAFT" if dirty else "ENFORCING";path,base=None,""
 for f in em("ListEnterpriseManagementResourceFiles",resourceId=rid,revision=rev).get("files",[]):
  if f.get("type")!="kgl":continue
  c=em("GetEnterpriseManagementResourceFile",resourceId=rid,type="kgl",path=f["path"],revision=rev)["content"]
  if path is None or S.search(c):path,base=f["path"],c
  if S.search(c):break
 if guard and not guard(base):return rid,"not ours"
 new=merge(base,kv)
 if new.rstrip("\n")==base.rstrip("\n"):return rid,"unchanged"
 a={"resourceId":rid,"files":[{"path":path or "policy.yaml","type":"kgl","content":new}]}
 if dr:a["ifMatch"]=dr["eTag"]
 et=em("PutEnterpriseManagementResourceFiles",**a)["eTag"];x={}
 if p.get("IdentityStoreId"):x["idcConfig"]={"identityStoreId":p["IdentityStoreId"],"region":R,"profileArn":p["ProfileArn"]}
 v=em("ValidateEnterpriseManagementResource",target={"byResource":rid},**x)
 if not v.get("isValid") or not publish:
  if not dirty:em("DeleteEnterpriseManagementResourceDraft",resourceId=rid,ifMatch=et)
  if not v.get("isValid"):raise Exception("validate failed: "+json.dumps(v.get("diagnostics",[])[:3]))
  return rid,"validated"
 return rid,str(em("PublishEnterpriseManagementResource",resourceId=rid,ifMatch=et,**x)["revision"])
def ready(arns):  # wait until the first rotation has put a real key into both secrets
 s=botocore.session.get_session().create_client("secretsmanager",region_name=R)
 for _ in range(48):
  if all(s.describe_secret(SecretId=a).get("LastRotatedDate") for a in arns):return
  time.sleep(5)
 raise Exception("the secrets were not rotated within 4 minutes; check the KeyFunction logs")
def send(ev,cx,st,pid,data,why=""):
 b=json.dumps({"Status":st,"PhysicalResourceId":pid,"Data":data,"Reason":why or cx.log_stream_name,"StackId":ev["StackId"],"RequestId":ev["RequestId"],"LogicalResourceId":ev["LogicalResourceId"]}).encode()
 urllib.request.urlopen(urllib.request.Request(ev["ResponseURL"],data=b,method="PUT",headers={"Content-Type":""}),timeout=30)
def handler(ev,cx):
 p=ev["ResourceProperties"];pid=ev.get("PhysicalResourceId","none");E,M=p["EventsSecretArn"],p["MetricsSecretArn"]
 try:
  if ev["RequestType"]=="Delete":
   if p.get("OnDelete")=="disable":
    try:print(apply(p,{"user_activity_report.otel_export":"false","realtime_event_export.enabled":"false"},guard=lambda t:E in t or M in t))
    except Exception as e:print("delete: left the policy as is:",e)
   return send(ev,cx,"SUCCESS",pid,{})
  ready([E,M])
  rid,r=apply(p,{"user_activity_report.enabled":"true","user_activity_report.otel_export":"true","user_activity_report.otel_secret_arn":'"%s"'%M,"user_activity_report.otel_protocol":"http/protobuf","user_activity_report.otel_temporality":"delta","realtime_event_export.enabled":"true","realtime_event_export.secret_arn":'"%s"'%E})
  print(rid,r);send(ev,cx,"SUCCESS",rid,{"PolicyId":rid,"Result":r})
 except Exception as e:
  print(e);send(ev,cx,"FAILED",pid,{},str(e)[:500])
'''.strip("\n")

# ----------------------------------------------------------------------------------- template
TEMPLATE = r'''AWSTemplateFormatVersion: "2010-09-09"
Description: >-
  Kiro Enterprise observability on Amazon CloudWatch: the KMS key, log group, ingest users, automatically
  rotated CloudWatch API keys, the Secrets Manager secrets that Kiro reads, a usage dashboard and saved
  Logs Insights queries. With ConfigureKiroPolicy=true it also merges only the OpenTelemetry export
  settings into your Kiro Controls policy and publishes it.

Metadata:
  AWS::CloudFormation::Interface:
    ParameterGroups:
      - Label: {default: Names}
        Parameters: [NamePrefix, DashboardName]
      - Label: {default: Real-time events log group}
        Parameters: [CreateLogGroup, LogGroupName, LogStreamName, RetentionInDays]
      - Label: {default: Kiro Controls policy (only the observability settings are changed)}
        Parameters: [ConfigureKiroPolicy, KiroPolicyId, KiroProfileId, KiroIdentityStoreId, KiroOnDelete, AllowExistingDraft]

Parameters:
  NamePrefix:
    Type: String
    Default: kiro-otel
    AllowedPattern: "[a-z0-9-]{1,40}"
    Description: Prefix for the KMS alias and the secret names.
  DashboardName:
    Type: String
    Default: ""
    Description: Empty = Kiro-Usage-<Region>. Dashboards are global, so each Region's stack needs its own name.
  CreateLogGroup:
    Type: String
    Default: "true"
    AllowedValues: ["true", "false"]
    Description: false = use an existing log group that already has bearer token authentication and the log stream.
  LogGroupName:
    Type: String
    Default: /kiro/otel-events
  LogStreamName:
    Type: String
    Default: events
  RetentionInDays:
    Type: Number
    Default: 30
    AllowedValues: [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653]
  ConfigureKiroPolicy:
    Type: String
    Default: "true"
    AllowedValues: ["true", "false"]
    Description: >-
      true = merge the seven OpenTelemetry export keys into your Kiro Controls policy and publish it.
      Nothing else in the policy changes. false = add them yourself from the KglSettings output.
  KiroPolicyId:
    Type: String
    Default: ""
    Description: Controls policy (resource) ID. Empty = the one policy assigned to a Kiro profile.
  KiroProfileId:
    Type: String
    Default: ""
    Description: Kiro profile ID. Needed only if several profiles have policies, or together with KiroIdentityStoreId.
  KiroIdentityStoreId:
    Type: String
    Default: ""
    Description: Only if your policy has rules with a when condition on users or groups, for example d-1234567890.
  KiroOnDelete:
    Type: String
    Default: disable
    AllowedValues: [disable, keep]
    Description: On stack delete, turn the two OpenTelemetry exports off (only if they still point at this stack's secrets).
  AllowExistingDraft:
    Type: String
    Default: "false"
    AllowedValues: ["true", "false"]
    Description: true = publish even if the policy draft holds someone's unpublished changes (they would go live too).

Rules:
  KiroRegion:
    Assertions:
      - Assert: !Contains [["us-east-1", "eu-central-1"], !Ref "AWS::Region"]
        AssertDescription: Deploy in the Region of your Kiro profile, us-east-1 or eu-central-1.

Conditions:
  MakeLogGroup: !Equals [!Ref CreateLogGroup, "true"]
  DoKiro: !Equals [!Ref ConfigureKiroPolicy, "true"]
  DefaultDashboardName: !Equals [!Ref DashboardName, ""]

Resources:
  # ------------------------------------------------------------------ encryption
  Key:
    Type: AWS::KMS::Key
    Properties:
      Description: Kiro OpenTelemetry export secrets
      EnableKeyRotation: true
      KeyPolicy:
        Version: "2012-10-17"
        Statement:
          - Sid: EnableRoot
            Effect: Allow
            Principal: {AWS: !Sub "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"}
            Action: "kms:*"
            Resource: "*"
          - Sid: AllowKiroDecryptViaSecretsManager
            Effect: Allow
            Principal: {Service: q.amazonaws.com}
            Action: [kms:Decrypt, kms:DescribeKey]
            Resource: "*"
            Condition:
              StringEquals: {kms:ViaService: !Sub "secretsmanager.${AWS::Region}.amazonaws.com"}
  KeyAlias:
    Type: AWS::KMS::Alias
    Properties:
      AliasName: !Sub "alias/${NamePrefix}-export"
      TargetKeyId: !Ref Key

  # ------------------------------------------------------------------ real-time events
  LogGroup:
    Type: AWS::Logs::LogGroup
    Condition: MakeLogGroup
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Properties:
      LogGroupName: !Ref LogGroupName
      RetentionInDays: !Ref RetentionInDays
      BearerTokenAuthenticationEnabled: true
  LogStream:
    Type: AWS::Logs::LogStream
    Condition: MakeLogGroup
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Properties:
      LogGroupName: !Ref LogGroup
      LogStreamName: !Ref LogStreamName

  # ------------------------------------------------------------------ ingest identities (one API key each)
  # No UserName: IAM names are global, and CloudFormation-generated names let the stack run in
  # several Regions of the same account (one per Kiro profile).
  LogsUser:
    Type: AWS::IAM::User
    Properties:
      Tags: [{Key: purpose, Value: kiro-otel}]
      Policies:
        - PolicyName: ingest
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action: logs:CallWithBearerToken
                Resource: "*"
              - Effect: Allow
                Action: logs:PutLogEvents
                Resource: !Sub "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:${LogGroupName}:log-stream:*"
  MetricsUser:
    Type: AWS::IAM::User
    Properties:
      Tags: [{Key: purpose, Value: kiro-otel}]
      Policies:
        - PolicyName: ingest
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action: [cloudwatch:CallWithBearerToken, cloudwatch:PutMetricData]
                Resource: "*"

  # ------------------------------------------------------------------ secrets Kiro reads
  EventsSecret:
    Type: AWS::SecretsManager::Secret
    Properties:
      Name: !Sub "${NamePrefix}/events"
      Description: Kiro real-time events to CloudWatch Logs. Value is written and rotated by the KeyFunction.
      KmsKeyId: !Ref Key
      SecretString: '{"pending": "the first rotation writes the real value"}'
  MetricsSecret:
    Type: AWS::SecretsManager::Secret
    Properties:
      Name: !Sub "${NamePrefix}/metrics"
      Description: Kiro daily metrics to CloudWatch OTLP metrics. Value is written and rotated by the KeyFunction.
      KmsKeyId: !Ref Key
      SecretString: '{"pending": "the first rotation writes the real value"}'
  EventsSecretPolicy:
    Type: AWS::SecretsManager::ResourcePolicy
    Properties:
      SecretId: !Ref EventsSecret
      BlockPublicPolicy: true
      ResourcePolicy:
        Version: "2012-10-17"
        Statement:
          - Sid: AllowKiroGetSecretValue
            Effect: Allow
            Principal: {Service: q.amazonaws.com}
            Action: secretsmanager:GetSecretValue
            Resource: "*"
  MetricsSecretPolicy:
    Type: AWS::SecretsManager::ResourcePolicy
    Properties:
      SecretId: !Ref MetricsSecret
      BlockPublicPolicy: true
      ResourcePolicy:
        Version: "2012-10-17"
        Statement:
          - Sid: AllowKiroGetSecretValue
            Effect: Allow
            Principal: {Service: q.amazonaws.com}
            Action: secretsmanager:GetSecretValue
            Resource: "*"

  # ------------------------------------------------------------------ API keys: created and rotated by Secrets Manager
  KeyFunctionRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal: {Service: lambda.amazonaws.com}
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - !Sub "arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
      Policies:
        - PolicyName: keys
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action: [iam:ListServiceSpecificCredentials, iam:CreateServiceSpecificCredential, iam:DeleteServiceSpecificCredential]
                Resource: [!GetAtt LogsUser.Arn, !GetAtt MetricsUser.Arn]
              - Effect: Allow
                Action:
                  - secretsmanager:DescribeSecret
                  - secretsmanager:GetSecretValue
                  - secretsmanager:PutSecretValue
                  - secretsmanager:UpdateSecretVersionStage
                  - secretsmanager:TagResource
                Resource: [!Ref EventsSecret, !Ref MetricsSecret]
              - Effect: Allow
                Action: [kms:GenerateDataKey, kms:Decrypt]
                Resource: !GetAtt Key.Arn
  KeyFunction:
    Type: AWS::Lambda::Function
    Properties:
      Description: Creates and rotates the CloudWatch API keys in the Kiro secrets. Never logs a key.
      Runtime: python3.13
      Handler: index.handler
      Timeout: 120
      Role: !GetAtt KeyFunctionRole.Arn
      Environment:
        Variables:
          KEY_LIFETIME_DAYS: "@@KEY_LIFETIME_DAYS@@"
          CFG: !Sub
            - '{"${E}": {"user": "${LU}", "service": "logs.amazonaws.com", "endpoint": "https://logs.${AWS::Region}.amazonaws.com", "path": "/v1/logs", "headers": "Authorization=Bearer __TOKEN__,x-aws-log-group=${LogGroupName},x-aws-log-stream=${LogStreamName}"}, "${M}": {"user": "${MU}", "service": "cloudwatch.amazonaws.com", "endpoint": "https://monitoring.${AWS::Region}.amazonaws.com/v1/metrics", "path": "/v1/metrics", "headers": "Authorization=Bearer __TOKEN__"}}'
            - {E: !Ref EventsSecret, M: !Ref MetricsSecret, LU: !Ref LogsUser, MU: !Ref MetricsUser}
      Code:
        ZipFile: |
@@KEY_LAMBDA@@
  KeyFunctionPermission:
    Type: AWS::Lambda::Permission
    Properties:
      FunctionName: !Ref KeyFunction
      Action: lambda:InvokeFunction
      Principal: secretsmanager.amazonaws.com
      SourceAccount: !Ref AWS::AccountId
  KeyCleanup:
    Type: Custom::KeyCleanup
    DependsOn: [LogsUser, MetricsUser]
    Properties:
      ServiceToken: !GetAtt KeyFunction.Arn
      ServiceTimeout: 300
  EventsRotation:
    Type: AWS::SecretsManager::RotationSchedule
    DependsOn: [KeyFunctionPermission, KeyCleanup, EventsSecretPolicy]
    Properties:
      SecretId: !Ref EventsSecret
      RotationLambdaARN: !GetAtt KeyFunction.Arn
      RotateImmediatelyOnUpdate: true
      RotationRules: {AutomaticallyAfterDays: @@ROTATE_EVERY_DAYS@@}
  MetricsRotation:
    Type: AWS::SecretsManager::RotationSchedule
    DependsOn: [KeyFunctionPermission, KeyCleanup, MetricsSecretPolicy]
    Properties:
      SecretId: !Ref MetricsSecret
      RotationLambdaARN: !GetAtt KeyFunction.Arn
      RotateImmediatelyOnUpdate: true
      RotationRules: {AutomaticallyAfterDays: @@ROTATE_EVERY_DAYS@@}

  # ------------------------------------------------------------------ Kiro Controls policy
  KiroPolicyRole:
    Type: AWS::IAM::Role
    Condition: DoKiro
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal: {Service: lambda.amazonaws.com}
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - !Sub "arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
      Policies:
        - PolicyName: kiro-controls
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              # IAM rejects resource ARNs for these preview actions ("failed legacy parsing"),
              # so access is limited by the action list instead.
              - Effect: Allow
                Action:
                  - q:ListEnterpriseManagementResources
                  - q:GetEnterpriseManagementResource
                  - q:ListEnterpriseManagementResourceFiles
                  - q:GetEnterpriseManagementResourceFile
                  - q:PutEnterpriseManagementResourceFiles
                  - q:ValidateEnterpriseManagementResource
                  - q:DeleteEnterpriseManagementResourceDraft
                  - q:PublishEnterpriseManagementResource
                Resource: "*"
              - Effect: Allow
                Action: secretsmanager:DescribeSecret
                Resource: [!Ref EventsSecret, !Ref MetricsSecret]
  KiroPolicyFunction:
    Type: AWS::Lambda::Function
    Condition: DoKiro
    Properties:
      Description: Merges only the Kiro OpenTelemetry export settings into the Controls policy, validates and publishes it.
      Runtime: python3.13
      Handler: index.handler
      Timeout: 300
      Role: !GetAtt KiroPolicyRole.Arn
      Code:
        ZipFile: |
@@KIRO_LAMBDA@@
  KiroPolicy:
    Type: Custom::KiroObservabilitySettings
    Condition: DoKiro
    DependsOn: [EventsRotation, MetricsRotation]
    Properties:
      ServiceToken: !GetAtt KiroPolicyFunction.Arn
      ServiceTimeout: 600
      EventsSecretArn: !Ref EventsSecret
      MetricsSecretArn: !Ref MetricsSecret
      PolicyId: !Ref KiroPolicyId
      ProfileId: !Ref KiroProfileId
      ProfileArn: !Sub "arn:${AWS::Partition}:codewhisperer:${AWS::Region}:${AWS::AccountId}:profile/${KiroProfileId}"
      IdentityStoreId: !Ref KiroIdentityStoreId
      OnDelete: !Ref KiroOnDelete
      AllowExistingDraft: !Ref AllowExistingDraft

  # ------------------------------------------------------------------ viewing
  Dashboard:
    Type: AWS::CloudWatch::Dashboard
    Properties:
      DashboardName: !If [DefaultDashboardName, !Sub "Kiro-Usage-${AWS::Region}", !Ref DashboardName]
      DashboardBody: !Sub
        - |
@@DASHBOARD@@
        - LogGroupResolved: !If [MakeLogGroup, !Ref LogGroup, !Ref LogGroupName]
@@QUERIES@@
Outputs:
  DashboardUrl:
    Description: Open this to see Kiro usage
    Value: !Sub "https://${AWS::Region}.console.aws.amazon.com/cloudwatch/home?region=${AWS::Region}#dashboards/dashboard/${Dashboard}"
  LogQueriesUrl:
    Description: Logs Insights; the saved queries are in the folder named after the dashboard
    Value: !Sub "https://${AWS::Region}.console.aws.amazon.com/cloudwatch/home?region=${AWS::Region}#logsV2:logs-insights"
  EventsSecretArn:
    Description: Kiro Controls > OpenTelemetry real-time events > Event export credentials
    Value: !Ref EventsSecret
  MetricsSecretArn:
    Description: Kiro Controls > OpenTelemetry metrics > OpenTelemetry credentials (HTTP/protobuf, delta)
    Value: !Ref MetricsSecret
  KeyRotation:
    Description: How the CloudWatch API keys are rotated
    Value: "Every @@ROTATE_EVERY_DAYS@@ days by Secrets Manager; each key lives @@KEY_LIFETIME_DAYS@@ days; the previous key stays valid until the next rotation"
  KiroPolicyId:
    Condition: DoKiro
    Description: Kiro Controls policy that the stack configured
    Value: !GetAtt KiroPolicy.PolicyId
  KiroPolicyResult:
    Condition: DoKiro
    Description: Published revision number, or "unchanged" if the policy already had these settings
    Value: !GetAtt KiroPolicy.Result
  KglSettings:
    Description: The keys the stack puts under settings (to do it by hand, set ConfigureKiroPolicy=false)
    Value: !Sub >-
      user_activity_report.enabled: true |
      user_activity_report.otel_export: true |
      user_activity_report.otel_secret_arn: "${MetricsSecret}" |
      user_activity_report.otel_protocol: http/protobuf |
      user_activity_report.otel_temporality: delta |
      realtime_event_export.enabled: true |
      realtime_event_export.secret_arn: "${EventsSecret}"
'''


def indent(text, n):
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def dashboard_text():
    """One widget per line: readable diffs, and the template stays under CloudFormation's
    51,200-byte limit for a template passed inline (aws cloudformation deploy without --s3-bucket)."""
    d = dashboard()
    head = {k: v for k, v in d.items() if k != "widgets"}
    lines = [json.dumps(w, ensure_ascii=False) for w in d["widgets"]]
    return (json.dumps(head, ensure_ascii=False)[:-1] + ', "widgets": [\n  '
            + ",\n  ".join(lines) + "\n]}")


def template():
    t = TEMPLATE
    for k, v in {"@@KEY_LAMBDA@@": indent(KEY_LAMBDA, 10), "@@KIRO_LAMBDA@@": indent(KIRO_LAMBDA, 10),
                 "@@DASHBOARD@@": indent(dashboard_text(), 10),
                 "@@ROTATE_EVERY_DAYS@@": str(ROTATE_EVERY_DAYS),
                 "@@KEY_LIFETIME_DAYS@@": str(KEY_LIFETIME_DAYS)}.items():
        t = t.replace(k, v)
    q = []
    for i, (name, lines) in enumerate(QUERIES):
        q.append("  Query%d:\n    Type: AWS::Logs::QueryDefinition\n    Properties:\n"
                 "      Name: !Sub [%s, {D: !Ref Dashboard}]\n"
                 "      LogGroupNames: [!If [MakeLogGroup, !Ref LogGroup, !Ref LogGroupName]]\n"
                 "      QueryString: %s" % (i + 1, json.dumps("${D}/" + name), json.dumps("\n| ".join(lines))))
    return t.replace("@@QUERIES@@", "\n".join(q) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "template":
        sys.stdout.write(template())
    elif len(sys.argv) == 4 and sys.argv[1] == "dashboard":
        print(json.dumps(dashboard(sys.argv[2], sys.argv[3]), ensure_ascii=False))
    else:
        sys.exit(__doc__)
