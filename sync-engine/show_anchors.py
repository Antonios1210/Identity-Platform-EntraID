import graph
ext = graph.config()["extension"]
users = graph.collect(f"/users?$select=displayName,userPrincipalName,employeeId,{ext}&$top=999")
print(f"{'workerId':<9}{'employeeId':<12}UPN")
for u in sorted(users, key=lambda u: u.get(ext) or ""):
    if u.get(ext):
        print(f"{u[ext]:<9}{u.get('employeeId') or '(none)':<12}{u['userPrincipalName']}")
