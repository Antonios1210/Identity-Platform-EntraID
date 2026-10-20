import graph

cfg = graph.config()
ext = cfg["extension"]
groups = {g["id"]: g["displayName"]
          for g in graph.collect("/groups?$select=id,displayName&$top=999")}
users = graph.collect(
    f"/users?$select=displayName,{ext},licenseAssignmentStates&$top=999")

for u in users:
    if not u.get(ext):
        continue
    states = u.get("licenseAssignmentStates") or []
    if not states:
        print(f"{u['displayName']:<28} (no license)")
    for s in states:
        gid = s.get("assignedByGroup")
        via = groups.get(gid, gid) if gid else "DIRECT"
        print(f"{u['displayName']:<28} {via:<24} {s.get('state')}")
