import graph, json, pathlib
state = json.loads(pathlib.Path("../state/tenant-objects.json").read_text())
gid = state["licensed_group_id"]
g = graph.call("GET", f"/groups/{gid}?$select=displayName,assignedLicenses,licenseProcessingState")
print(json.dumps(g, indent=2))
errs = graph.collect(f"/groups/{gid}/membersWithLicenseErrors?$select=displayName")
print("members with license errors:", [e["displayName"] for e in errs])
